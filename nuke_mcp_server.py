from fastmcp import FastMCP, Context
import socket
import json
import asyncio
import logging
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional, Union

# Configure logging
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NukeMCPServer")

@dataclass
class NukeConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self) -> bool:
        """Connect to the Nuke addon socket server"""
        # Always close any existing connection first
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
            
        try:
            logger.debug(f"Attempting to connect to Nuke at {self.host}:{self.port}")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5)  # Set a timeout for the connection attempt
            self.sock.connect((self.host, self.port))
            logger.info(f"Successfully connected to Nuke at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Nuke: {str(e)}")
            if self.sock:
                try:
                    self.sock.close()
                except:
                    pass
                self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Nuke addon"""
        if self.sock:
            try:
                logger.debug("Closing socket connection to Nuke")
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Nuke: {str(e)}")
            finally:
                self.sock = None
                logger.info("Disconnected from Nuke")

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Set a timeout for receiving response
        sock.settimeout(15.0)
        
        try:
            logger.debug("Waiting to receive data from Nuke...")
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    logger.debug(f"Received chunk of {len(chunk)} bytes")
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        logger.debug("Incomplete JSON, continuing to receive...")
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Nuke and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Nuke")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        try:
            # Log the command being sent
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            command_json = json.dumps(command)
            logger.debug(f"Raw command JSON: {command_json}")
            self.sock.sendall(command_json.encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set a timeout for receiving
            self.sock.settimeout(15.0)
            
            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Nuke error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Nuke"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Nuke")
            # Invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception("Timeout waiting for Nuke response - try simplifying your request")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Nuke lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Nuke: {str(e)}")
            # Try to log what was received
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Nuke: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Nuke: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Nuke: {str(e)}")

# Global connection for resources
_nuke_connection = None

def get_nuke_connection():
    """Get or create a persistent Nuke connection"""
    global _nuke_connection
    
    # If we have an existing connection, check if it's still valid
    if _nuke_connection is not None:
        try:
            # Try a simple ping command to check if the connection is still valid
            logger.debug("Testing existing connection with a ping")
            _nuke_connection.send_command("get_script_info")
            logger.debug("Existing connection is valid")
            return _nuke_connection
        except Exception as e:
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _nuke_connection.disconnect()
            except:
                pass
            _nuke_connection = None
    
    # Create a new connection
    logger.info("Creating new connection to Nuke")
    _nuke_connection = NukeConnection(host="localhost", port=9876)
    
    # Try connecting multiple times with a delay
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Connection attempt {attempt}/{max_attempts}")
        if _nuke_connection.connect():
            logger.info("Successfully connected to Nuke")
            
            # Verify connection with a simple command
            try:
                logger.debug("Verifying connection with a test command")
                _nuke_connection.send_command("get_script_info")
                logger.info("Connection verified - Nuke is responding to commands")
                return _nuke_connection
            except Exception as e:
                logger.error(f"Connection verification failed: {str(e)}")
                _nuke_connection.disconnect()
        
        if attempt < max_attempts:
            delay = 2 * attempt  # Increasing delay between attempts
            logger.info(f"Waiting {delay} seconds before next attempt")
            time.sleep(delay)
    
    # If we get here, all connection attempts failed
    logger.error("Failed to connect to Nuke after multiple attempts")
    _nuke_connection = None
    raise Exception("Could not connect to Nuke. Make sure the Nuke addon is running.")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        # Log that we're starting up
        logger.info("NukeMCP server starting up")
        logger.info("This server will connect to Nuke when a client makes a request")
        logger.info("Make sure Nuke is running with the addon active (NukeMCP panel with 'Running' status)")
        
        # We don't try to connect to Nuke on startup anymore
        # Instead, we'll connect when the first request comes in
        
        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _nuke_connection
        if _nuke_connection:
            logger.info("Disconnecting from Nuke on shutdown")
            _nuke_connection.disconnect()
            _nuke_connection = None
        logger.info("NukeMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "NukeMCP",
    description="Nuke integration through the Model Context Protocol",
    lifespan=server_lifespan
)

@mcp.tool()
def get_script_info(ctx: Context) -> str:
    """Get detailed information about the current Nuke script"""
    try:
        logger.info("Tool called: get_script_info")
        nuke = get_nuke_connection()
        result = nuke.send_command("get_script_info")
        
        # Format the response in a more human-readable way
        script_name = result.get("name", "Untitled")
        fps = result.get("fps", 0)
        format_info = result.get("format", "Unknown")
        first_frame = result.get("first_frame", 0)
        last_frame = result.get("last_frame", 0)
        nodes = result.get("nodes", [])
        
        # Create a summary of the script
        output = f"Script: {script_name}\n"
        output += f"Frame Range: {first_frame} - {last_frame} @ {fps} fps\n"
        output += f"Format: {format_info}\n\n"
        
        # Count node types
        node_types = {}
        for node in nodes:
            node_type = node.get("type", "Unknown")
            if node_type in node_types:
                node_types[node_type] += 1
            else:
                node_types[node_type] = 1
        
        output += f"Total Nodes: {len(nodes)}\n"
        output += "Node Types:\n"
        for node_type, count in sorted(node_types.items()):
            output += f"  - {node_type}: {count}\n"
        
        return output
    except Exception as e:
        logger.error(f"Error in get_script_info: {str(e)}")
        return f"Error getting script info: {str(e)}"

@mcp.tool()
def get_node_info(ctx: Context, node_name: str) -> str:
    """
    Get detailed information about a specific node in the Nuke script.
    
    Parameters:
    - node_name: The name of the node to get information about
    """
    try:
        logger.info(f"Tool called: get_node_info for node {node_name}")
        nuke = get_nuke_connection()
        result = nuke.send_command("get_node_info", {"name": node_name})
        
        # Format the response in a more human-readable way
        node_type = result.get("type", "Unknown")
        position = result.get("position", [0, 0])
        inputs = result.get("inputs", [])
        parameters = result.get("parameters", {})
        
        output = f"Node: {node_name} ({node_type})\n"
        output += f"Position: X={position[0]}, Y={position[1]}\n\n"
        
        # Show inputs
        output += "Inputs:\n"
        if inputs:
            for i, input_info in enumerate(inputs):
                if input_info:
                    output += f"  {i}: {input_info.get('name')} ({input_info.get('type')})\n"
                else:
                    output += f"  {i}: None\n"
        else:
            output += "  None\n"
        
        # Show parameters
        output += "\nParameters:\n"
        if parameters:
            for name, param in sorted(parameters.items()):
                value = param.get("value", "")
                if isinstance(value, list):
                    value_str = ", ".join(str(v) for v in value)
                    output += f"  {name}: [{value_str}]\n"
                else:
                    output += f"  {name}: {value}\n"
        else:
            output += "  No visible parameters\n"
        
        return output
    except Exception as e:
        logger.error(f"Error in get_node_info: {str(e)}")
        return f"Error getting node info: {str(e)}"

@mcp.tool()
def create_node(
    ctx: Context,
    node_type: str,
    name: str = None,
    position: List[int] = None,
    inputs: List[str] = None,
    parameters: Dict[str, Any] = None
) -> str:
    """
    Create a new node in the Nuke script.
    
    Parameters:
    - node_type: Type of node to create (e.g., "Blur", "Grade", "Merge2")
    - name: Optional name for the new node
    - position: Optional [x, y] position coordinates
    - inputs: Optional list of node names to connect as inputs
    - parameters: Optional dictionary of parameter name/value pairs
    """
    try:
        logger.info(f"Tool called: create_node of type {node_type}")
        nuke = get_nuke_connection()
        result = nuke.send_command("create_node", {
            "node_type": node_type,
            "name": name,
            "position": position,
            "inputs": inputs,
            "parameters": parameters
        })
        
        actual_name = result.get("name", "unknown")
        return f"Created {node_type} node named '{actual_name}'"
    except Exception as e:
        logger.error(f"Error in create_node: {str(e)}")
        return f"Error creating node: {str(e)}"

@mcp.tool()
def modify_node(
    ctx: Context,
    name: str,
    parameters: Dict[str, Any] = None,
    position: List[int] = None,
    inputs: List[str] = None
) -> str:
    """
    Modify an existing node in the Nuke script.
    
    Parameters:
    - name: Name of the node to modify
    - parameters: Optional dictionary of parameter name/value pairs
    - position: Optional [x, y] position coordinates
    - inputs: Optional list of node names to connect as inputs
    """
    try:
        logger.info(f"Tool called: modify_node for node {name}")
        nuke = get_nuke_connection()
        result = nuke.send_command("modify_node", {
            "name": name,
            "parameters": parameters,
            "position": position,
            "inputs": inputs
        })
        
        modified_params = []
        if parameters:
            modified_params.extend(parameters.keys())
        
        if position:
            modified_params.append("position")
        
        if inputs:
            modified_params.append("inputs")
        
        if modified_params:
            return f"Modified node '{name}' - updated: {', '.join(modified_params)}"
        else:
            return f"Node '{name}' unchanged - no modifications specified"
    except Exception as e:
        logger.error(f"Error in modify_node: {str(e)}")
        return f"Error modifying node: {str(e)}"

@mcp.tool()
def delete_node(ctx: Context, name: str) -> str:
    """
    Delete a node from the Nuke script.
    
    Parameters:
    - name: Name of the node to delete
    """
    try:
        logger.info(f"Tool called: delete_node for node {name}")
        nuke = get_nuke_connection()
        result = nuke.send_command("delete_node", {"name": name})
        
        deleted_name = result.get("deleted", name)
        node_type = result.get("type", "unknown")
        return f"Deleted {node_type} node '{deleted_name}'"
    except Exception as e:
        logger.error(f"Error in delete_node: {str(e)}")
        return f"Error deleting node: {str(e)}"

@mcp.tool()
def position_node(ctx: Context, name: str, x: int, y: int) -> str:
    """
    Position a node at specific coordinates in the Nuke node graph.
    
    Parameters:
    - name: Name of the node to position
    - x: X coordinate in the node graph
    - y: Y coordinate in the node graph
    """
    try:
        logger.info(f"Tool called: position_node for node {name} at ({x}, {y})")
        nuke = get_nuke_connection()
        result = nuke.send_command("position_node", {
            "name": name,
            "position": [x, y]
        })
        
        return f"Positioned node '{name}' at X={x}, Y={y}"
    except Exception as e:
        logger.error(f"Error in position_node: {str(e)}")
        return f"Error positioning node: {str(e)}"

@mcp.tool()
def connect_nodes(ctx: Context, output_node: str, input_node: str, input_index: int = 0) -> str:
    """
    Connect nodes together in the Nuke script.
    
    Parameters:
    - output_node: Name of the node whose output to connect
    - input_node: Name of the node to connect the output to
    - input_index: Input index on the receiving node (default: 0)
    """
    try:
        logger.info(f"Tool called: connect_nodes from {output_node} to {input_node} at index {input_index}")
        nuke = get_nuke_connection()
        result = nuke.send_command("connect_nodes", {
            "output_node": output_node,
            "input_node": input_node,
            "input_index": input_index
        })
        
        return f"Connected output of '{output_node}' to input {input_index} of '{input_node}'"
    except Exception as e:
        logger.error(f"Error in connect_nodes: {str(e)}")
        return f"Error connecting nodes: {str(e)}"

@mcp.tool()
def render(
    ctx: Context,
    frame_range: str = None,
    write_node: str = None,
    proxy_mode: bool = False
) -> str:
    """
    Render frames from the Nuke script.
    
    Parameters:
    - frame_range: Range of frames to render (e.g., "1-10" or "1,3,5-10")
    - write_node: Optional name of Write node to render (if None, renders all)
    - proxy_mode: Whether to render in proxy mode
    """
    try:
        logger.info(f"Tool called: render with range {frame_range}, write_node: {write_node}")
        nuke = get_nuke_connection()
        result = nuke.send_command("render", {
            "frame_range": frame_range,
            "write_node": write_node,
            "proxy_mode": proxy_mode
        })
        
        status = result.get("status", "Rendering completed")
        return f"{status}"
    except Exception as e:
        logger.error(f"Error in render: {str(e)}")
        return f"Error initiating render: {str(e)}"

@mcp.tool()
def viewer_playback(
    ctx: Context,
    action: str = "play",
    start_frame: int = None,
    end_frame: int = None
) -> str:
    """
    Control Nuke's Viewer playback.
    
    Parameters:
    - action: Playback action (play, stop, next, prev)
    - start_frame: Optional starting frame for playback
    - end_frame: Optional ending frame for playback
    """
    try:
        logger.info(f"Tool called: viewer_playback with action {action}")
        nuke = get_nuke_connection()
        result = nuke.send_command("viewer_playback", {
            "action": action,
            "start_frame": start_frame,
            "end_frame": end_frame
        })
        
        status = result.get("status", "Viewer operation completed")
        return status
    except Exception as e:
        logger.error(f"Error in viewer_playback: {str(e)}")
        return f"Error controlling viewer: {str(e)}"

@mcp.tool()
def execute_nuke_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in Nuke.
    
    Parameters:
    - code: The Python code to execute
    """
    try:
        logger.info(f"Tool called: execute_nuke_code with code length {len(code)}")
        nuke = get_nuke_connection()
        result = nuke.send_command("execute_code", {"code": code})
        
        if result.get("executed", False):
            output = result.get("output", {})
            if output:
                # Format any output from the executed code
                output_str = "\n".join(f"{k}: {v}" for k, v in output.items())
                return f"Code executed successfully with output:\n{output_str}"
            else:
                return "Code executed successfully"
        else:
            return "Code execution failed"
    except Exception as e:
        logger.error(f"Error in execute_nuke_code: {str(e)}")
        return f"Error executing code: {str(e)}"

@mcp.tool()
def auto_layout_nodes(ctx: Context, selected_only: bool = False) -> str:
    """
    Automatically arrange nodes in the Nuke script for better readability.
    
    Parameters:
    - selected_only: Only arrange currently selected nodes if True
    """
    try:
        logger.info(f"Tool called: auto_layout_nodes with selected_only={selected_only}")
        nuke = get_nuke_connection()
        result = nuke.send_command("auto_layout", {
            "selected_only": selected_only
        })
        
        status = result.get("status", "Nodes arranged")
        return status
    except Exception as e:
        logger.error(f"Error in auto_layout_nodes: {str(e)}")
        return f"Error arranging nodes: {str(e)}"

@mcp.tool()
def set_frames(
    ctx: Context,
    first_frame: int = None,
    last_frame: int = None,
    current_frame: int = None
) -> str:
    """
    Set the frame range and current frame in the Nuke script.
    
    Parameters:
    - first_frame: New value for first frame
    - last_frame: New value for last frame
    - current_frame: New value for current frame
    """
    try:
        logger.info(f"Tool called: set_frames")
        nuke = get_nuke_connection()
        result = nuke.send_command("set_frames", {
            "first_frame": first_frame,
            "last_frame": last_frame,
            "current_frame": current_frame
        })
        
        return f"Updated frame settings - First: {result['first_frame']}, Last: {result['last_frame']}, Current: {result['current_frame']}"
    except Exception as e:
        logger.error(f"Error in set_frames: {str(e)}")
        return f"Error setting frames: {str(e)}"

@mcp.tool()
def create_viewer(ctx: Context, input_node: str = None) -> str:
    """
    Create a Viewer node connected to the specified input node.
    
    Parameters:
    - input_node: Optional name of node to connect to the Viewer
    """
    try:
        logger.info(f"Tool called: create_viewer connected to {input_node}")
        nuke = get_nuke_connection()
        result = nuke.send_command("create_viewer", {
            "input_node": input_node
        })
        
        viewer_name = result.get("name", "Viewer")
        if input_node:
            return f"Created Viewer node '{viewer_name}' connected to '{input_node}'"
        else:
            return f"Created Viewer node '{viewer_name}'"
    except Exception as e:
        logger.error(f"Error in create_viewer: {str(e)}")
        return f"Error creating viewer: {str(e)}"

@mcp.prompt()
def nuke_mcp_usage() -> str:
    """Provides guidance on how to use the Nuke MCP tools"""
    return """# Working with Nuke through MCP

When creating or editing composites in Nuke, follow these guidelines:

## Getting Started
1. First, use `get_script_info()` to understand the current state of the Nuke script
2. For details on specific nodes, use `get_node_info(node_name="NodeName")`

## Creating and Connecting Nodes
1. Create nodes with `create_node(node_type="Type", parameters={...})`
2. Connect nodes with `connect_nodes(output_node="Source", input_node="Target", input_index=0)`
3. For complex node trees, create nodes first, then connect them

## Node Placement
1. When creating multiple nodes, specify positions to avoid overlaps
2. For automatic arrangement, use `auto_layout_nodes()`
3. Position individual nodes with `position_node(name="NodeName", x=100, y=100)`

## Rendering and Viewing
1. Control playback with `viewer_playback(action="play")`
2. Render frames with `render(frame_range="1-10", write_node="Write1")`
3. Create viewers with `create_viewer(input_node="NodeName")`

## Best Practices
1. Always create Write nodes for output
2. Organize nodes spatially for clarity
3. Use descriptive node names
4. Group related nodes together in the node graph
"""

def main():
    """Run the NukeMCP server"""
    logger.info("Starting NukeMCP main function")
    logger.info("This server connects to Nuke and exposes MCP tools")
    logger.info("Make sure Nuke is running with the NukeMCP addon active")
    mcp.run()

if __name__ == "__main__":
    main()
