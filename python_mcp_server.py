import argparse
import base64
import hashlib
import json
import logging
import os
import tempfile
import urllib.request
from datetime import datetime
from textwrap import dedent
from typing import Dict

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

from fastmcp import FastMCP
from mcp.server import Server
from mcp.server.sse import SseServerTransport


class Config:
    MCP_SERVER_NAME = "python-sandbox-mcp-sse"
    SNEKBOX_URL = "http://localhost:8060/eval"

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMP_DIR = os.path.join(BASE_DIR, "temp")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(Config.MCP_SERVER_NAME)

mcp = FastMCP(Config.MCP_SERVER_NAME)


def get_unique_filename(content: str, prefix: str = "", suffix: str = "") -> str:
    """Generate a unique filename based on content and timestamp"""
    content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}{content_hash}_{timestamp}{suffix}"


@mcp.tool(
    name="execute_python_code",
    description="""
    Execute Python code in a secure sandbox environment, supporting the following features:
    1. Execute regular Python code and return standard output
    2. Execute plotting code (e.g., matplotlib) and save generated PNG images
    3. Provide detailed execution status and error information

    Return format:
    - Successful text output: {"status": "success", "output_type": "text", "stdout": "output content"}
    - Successful plot generation: {"status": "success", "output_type": "plot", "plot_path": "image path"}
    - Execution error: {"status": "error", "error_type": "error type", "error_message": "error details"}
    """
)
def execute_python_code(python_code: str) -> Dict:
    try:
        os.makedirs(Config.TEMP_DIR, exist_ok=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            main_filename = get_unique_filename(python_code, suffix=".py")
            main_file_path = os.path.join(temp_dir, main_filename)

            with open(main_file_path, "w", encoding="utf-8") as f:
                f.write(dedent(python_code).strip())

            data = {
                "args": [main_filename],
                "files": [
                    {
                        "path": main_filename,
                        "content": base64.b64encode(
                            open(main_file_path, "rb").read()
                        ).decode("utf-8"),
                    }
                ],
            }

            json_data = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(Config.SNEKBOX_URL)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("Content-Length", str(len(json_data)))

            with urllib.request.urlopen(req, json_data) as response:
                result = json.loads(response.read().decode("utf-8"))

            returncode = result.get("returncode", 0)
            stdout = result.get("stdout", "").strip()
            files = result.get("files", [])

            response = {
                "status": "success" if returncode == 0 else "error",
                "returncode": returncode
            }

            # Handle execution errors
            if returncode != 0:
                response.update({
                    "error_type": "execution_error",
                    "error_message": stdout,
                    "details": "Code execution encountered syntax error or runtime error"
                })
                return response

            # Handle text output
            if stdout:
                response.update({
                    "output_type": "text",
                    "stdout": stdout,
                    "details": "Code executed successfully and produced text output"
                })

            # Handle image output
            if files:
                for file in files:
                    if file["path"].endswith(".png"):
                        output_filename = get_unique_filename("", "plot_", ".png")
                        output_path = os.path.join(Config.TEMP_DIR, output_filename)

                        with open(output_path, "wb") as f:
                            f.write(base64.b64decode(file["content"]))

                        response.update({
                            "output_type": "plot",
                            "plot_data": file["content"],
                            "plot_path": output_path,
                            "details": "Code executed successfully and generated a plot"
                        })

            # Handle no output case
            if not stdout and not files:
                response.update({
                    "output_type": "none",
                    "details": "Code executed successfully but produced no output"
                })

            return response

    except Exception as e:
        logger.error(f"Error occurred while executing code: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "error_type": "system_error",
            "error_message": str(e),
            "details": "System exception occurred during execution"
        }


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    """Create a Starlette application with SSE support for running the MCP server."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


if __name__ == "__main__":
    mcp_server = mcp._mcp_server

    parser = argparse.ArgumentParser(description='Run SSE-based MCP Python Sandbox Server')
    parser.add_argument('--host', default='0.0.0.0', help='Binding host address')
    parser.add_argument('--port', type=int, default=18080, help='Listen port')
    args = parser.parse_args()

    starlette_app = create_starlette_app(mcp_server, debug=True)
    uvicorn.run(starlette_app, host=args.host, port=args.port)
