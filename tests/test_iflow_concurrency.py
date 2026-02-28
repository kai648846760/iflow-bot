import asyncio
import json
import subprocess
import time

async def main():
    print("Starting iflow process...")
    process = await asyncio.create_subprocess_exec(
        "uv", "run", "iflow", "--experimental-acp", "stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    responses = {}
    
    async def read_output():
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode().strip()
            print("STDOUT:", text)
            try:
                data = json.loads(text)
                if "id" in data:
                    responses[data["id"]] = data
            except:
                pass
            
    async def read_err():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            print("STDERR:", line.decode().strip())
            
    readers = asyncio.gather(read_output(), read_err())

    # Wait for ready signal
    await asyncio.sleep(2)
    
    init_req = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {
                    "readTextFile": True,
                    "writeTextFile": True,
                }
            }
        },
        "id": "init"
    }
    print("Sending init...")
    process.stdin.write((json.dumps(init_req) + "\n").encode())
    await process.stdin.drain()
    await asyncio.sleep(1)
    
    # Create session 1
    req_s1 = {
        "jsonrpc": "2.0",
        "method": "session/new",
        "params": {"cwd": ".", "mcpServers": []},
        "id": "s1"
    }
    process.stdin.write((json.dumps(req_s1) + "\n").encode())
    await process.stdin.drain()
    await asyncio.sleep(1)
    session_id_main = responses.get("s1", {}).get("result", {}).get("sessionId")
    
    # Create session 2
    req_s2 = {
        "jsonrpc": "2.0",
        "method": "session/new",
        "params": {"cwd": ".", "mcpServers": []},
        "id": "s2"
    }
    process.stdin.write((json.dumps(req_s2) + "\n").encode())
    await process.stdin.drain()
    await asyncio.sleep(1)
    session_id_sub = responses.get("s2", {}).get("result", {}).get("sessionId")
    
    print(f"Sessions created: {session_id_main}, {session_id_sub}")
    
    req1 = {
        "jsonrpc": "2.0",
        "method": "session/prompt",
        "params": {
            "sessionId": session_id_main,
            "prompt": [{"type": "text", "text": "count to 10 very slowly"}],
            "model": "minimax-m2.5"
        },
        "id": 1
    }
    
    req2 = {
        "jsonrpc": "2.0",
        "method": "session/prompt",
        "params": {
            "sessionId": session_id_sub,
            "prompt": [{"type": "text", "text": "what is 1+1?"}],
            "model": "minimax-m2.5"
        },
        "id": 2
    }
    
    # Send req1
    print(f"Sending req1 (long task on {session_id_main})...")
    process.stdin.write((json.dumps(req1) + "\n").encode())
    await process.stdin.drain()
    
    # Wait a bit
    await asyncio.sleep(2)
    
    # Send req2 with DIFFERENT session ID
    print(f"Sending req2 (concurrent prompt on {session_id_sub})...")
    process.stdin.write((json.dumps(req2) + "\n").encode())
    await process.stdin.drain()
    
    # Wait for both tasks to progress
    await asyncio.sleep(8)
    
    process.kill()
    try:
        await readers
    except asyncio.CancelledError:
        pass
    
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
