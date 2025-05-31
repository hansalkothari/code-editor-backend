from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sys
import io
import os
import traceback
import contextlib
import asyncio
import tempfile

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://code-sama.netlify.app","http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CodeRequest(BaseModel):
    code: str
    stdin: str = ""  # Allow input from frontend

@app.post("/run-code")
async def run_code(req: CodeRequest):
    code = req.code
    stdin_content = req.stdin

    try:
        # Redirect stdout and stdin
        old_stdout = sys.stdout
        old_stdin = sys.stdin
        sys.stdout = mystdout = io.StringIO()
        sys.stdin = io.StringIO(stdin_content)

        # Use a limited globals dict to restrict built-ins
        global_env = {}

        # Execute the code safely
        with contextlib.redirect_stdout(mystdout):
            exec(code, global_env)

        # Capture output
        output = mystdout.getvalue()

    except Exception:
        error_message = traceback.format_exc()
        return {"output": "", "error": error_message}

    finally:
        # Restore stdout and stdin
        sys.stdout = old_stdout
        sys.stdin = old_stdin

    return {"output": output, "error": ""}

@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    1) Wait for the client to send the Python code string.
    2) Write that code to a temporary file.
    3) Launch `python temp_code.py` as a subprocess.
    4) Forward subprocess stdout/stderr to the client.
    5) Forward client keystrokes (stdin) to subprocess.stdin.
    """

    await websocket.accept()

    try:
        # Step 1: Receive the user's code (as plain text) from the client
        packet = await websocket.receive_text()
        user_code = packet

        # Step 2: Write that code to a temporary file
        tmp_dir = tempfile.TemporaryDirectory()
        file_path = os.path.join(tmp_dir.name, "temp_code.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(user_code)

        # Step 3: Spawn the Python subprocess (using the same Python interpreter)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", file_path,      # ← add "-u" here
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_stream_and_send(stream, stream_type: str):
            """
            Reads from subprocess stdout or stderr and forwards it to the client.
            We prefix each message with a small header so the frontend can treat stderr differently if desired.
            """
            while True:
                data = await stream.read(1024)
                if not data:
                    break
                
                text = data.decode("utf-8", errors="replace")
                await websocket.send_json({
                    "type": stream_type,  
                    "payload": text
                })

        # Step 4: Launch tasks to read stdout and stderr concurrently
        stdout_task = asyncio.create_task(read_stream_and_send(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream_and_send(process.stderr, "stderr"))

        # Step 5: Meanwhile, read from WebSocket and write to subprocess.stdin
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                # Client disconnected—terminate subprocess and break
                process.kill()
                break

            # We expect client messages to be raw keystrokes to send to stdin.
            # For example, if the client sends "hello\n", we forward that to process.stdin.
            if process.stdin:
                try:
                    process.stdin.write(message.encode("utf-8"))
                    await process.stdin.drain()
                except Exception:
                    pass

            # Check if process is done
            if process.returncode is None:
                pass
            else:
                break

            # We also want to poll the subprocess’s returncode occasionally.
            await asyncio.sleep(0.01)

        # Wait for the stdout/stderr tasks to finish
        await stdout_task
        await stderr_task

        # Once the subprocess has finished, send a final “done” message
        if process.returncode is not None:
            await websocket.send_json({
                "type": "done",
                "payload": f"--- Process exited with code {process.returncode} ---\n"
            })

    except Exception as e:
        # If anything unexpected happens, send the traceback to the client
        import traceback
        tb = traceback.format_exc()
        try:
            await websocket.send_json({
                "type": "error",
                "payload": tb
            })
        except:
            pass

    finally:
        tmp_dir.cleanup()
        try:
            await websocket.close()
        except:
            pass
