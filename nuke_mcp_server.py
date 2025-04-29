from fastmcp import FastMCP, Context
import socket
import json
import asyncio
import logging
import time
import os
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional, Union

# Configure logging
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NukeMCPServer")

# Workflow Rules for Nuke Compositing
class NukeWorkflowRules:
    """Enforce professional compositing workflow rules for Nuke operations."""
    
    @staticmethod       
    def validate_node_creation(node_type, name=None, parameters=None):
        """Validate node creation against workflow rules."""
        issues = []
        
        # 1. Node Labeling Conventions - Never rename, always use labels
        if name:
            # Instead of suggesting name changes, always suggest using labels
            issues.append(f"Consider using the default node naming and adding descriptive labels instead of custom node names")
            
        # Add automatic labels based on node type if not present
        if parameters is None:
            parameters = {}
            
        # Handle specific node types with special labeling
        if node_type == "Write" and "label" not in parameters:
            output_name = os.path.basename(parameters.get("file", "output"))
            parameters["label"] = f"OUTPUT: {output_name}"
            
        if node_type == "Read" and "label" not in parameters:
            source_name = os.path.basename(parameters.get("file", "source"))
            parameters["label"] = f"INPUT: {source_name}"
            
        if node_type in ["Grade", "ColorCorrect"] and "label" not in parameters:
            parameters["label"] = "ADJUST: Color"
            
        if node_type in ["Transform", "Tracker"] and "label" not in parameters:
            parameters["label"] = "ADJUST: Transform"
            
        if node_type in ["Blur", "Defocus", "ZDefocus"] and "label" not in parameters:
            parameters["label"] = "FX: Blur"
            
        if node_type in ["Keyer", "Primatte", "IBKColour"] and "label" not in parameters:
            parameters["label"] = "KEY"
        
        # 2. Required Parameters Check
        if node_type == "Read" and parameters:
            if "file" not in parameters:
                issues.append("Read nodes should specify a 'file' parameter")
        
        if node_type == "Write" and parameters:
            if "file" not in parameters:
                issues.append("Write nodes should specify a 'file' parameter")
            if "create_directories" in parameters and not parameters["create_directories"]:
                issues.append("Write nodes should have 'create_directories' enabled")
        
        # 3. Color-related Node Rules
        color_correction_nodes = ["Grade", "ColorCorrect", "HueCorrect", "Saturation"]
        if node_type in color_correction_nodes:
            # Suggest unpremult before color correction
            issues.append("Consider adding Unpremult before color correction and Premult after")
            
        # 4. Filter Node Rules
        filter_nodes = ["Blur", "Defocus", "EdgeBlur"]
        if node_type in filter_nodes:
            # Suggest appropriate filter settings
            if node_type == "Blur" and parameters and "channels" in parameters:
                if parameters["channels"] == "all":
                    issues.append("Consider blurring only RGB channels, not alpha, unless specifically needed")
        
        return issues, parameters  # Return both issues and potentially modified parameters
    
    @staticmethod
    def validate_node_modification(name, parameters=None, position=None):
        """Validate node modification against workflow rules."""
        issues = []
        
        # 1. Don't rename nodes, use label instead
        if parameters and "name" in parameters:
            issues.append("Avoid renaming nodes. Use 'label' knob to add descriptive text instead.")
        
        # 2. Write Node Settings
        if name and (name.startswith("Write") or name.startswith("OUT_")) and parameters:
            if "create_directories" in parameters and not parameters["create_directories"]:
                issues.append("Write nodes should have 'create_directories' enabled.")
        
        return issues
    
    @staticmethod
    def validate_node_connection(output_node, input_node, input_index=0):
        """Validate node connections against workflow rules."""
        issues = []
        
        # 1. B-pipe Structure
        if input_index == 0 and "Merge" in input_node:
            issues.append("For Merge nodes, connect main pipeline to input B (1) to maintain B-pipe structure.")
        
        # 2. Color Correction Flow
        color_correction_nodes = ["Grade", "ColorCorrect", "HueCorrect", "Saturation"]
        if any(cc_node in output_node for cc_node in color_correction_nodes):
            issues.append("Check if Unpremult is needed before color correction.")
            
        return issues
    
    @staticmethod
    def validate_node_position(node, new_position):
        """Validate node positioning against workflow rules."""
        issues = []
        
        # Suggest top-to-bottom, left-to-right flow
        x, y = new_position
        
        # These are basic suggestions since we don't have context of other nodes
        issues.append("Maintain a top-to-bottom, left-to-right flow in the node graph.")
        issues.append("Consider using right angles for connection lines when possible.")
        
        return issues
    
    @staticmethod
    def get_node_type_suggestions(node_type):
        """Get workflow suggestions for specific node types."""
        suggestions = {
            "Merge": [
                "Use Merge nodes with 'over' operation for standard compositing",
                "Connect main pipeline to input B (1) for consistent B-pipe structure",
                "For operations like 'plus' or 'screen', consider using the appropriate operation"
            ],
            "Grade": [
                "Consider using Unpremult before and Premult after for premultiplied images",
                "Use white.a as mask to only affect specific areas",
                "Keep color corrections subtle and use multiple Grade nodes for different adjustments"
            ],
            "ColorCorrect": [
                "Split adjustments between shadows, midtones, and highlights",
                "Consider using Unpremult before and Premult after for premultiplied images"
            ],
            "Blur": [
                "Be specific about which channels to blur",
                "Consider using separate blur values for rgb and alpha when appropriate",
                "Use smaller blur sizes when possible for performance"
            ],
            "Transform": [
                "Use center controls to define rotation point",
                "Enable motion blur when animating transforms",
                "Consider 'black outside' vs 'format' settings based on needs"
            ],
            "Roto": [
                "Name shapes descriptively within the node",
                "Use motion blur when needed for moving objects",
                "Use feather instead of blur nodes when possible for shape edges"
            ],
            "Keyer": [
                "Despill after keying, not before",
                "Use core matte and edge adjustments in separate nodes",
                "Consider unpremultiplication when color correcting keyed elements"
            ]
        }
        
        return suggestions.get(node_type, [])
    
    @staticmethod
    def apply_auto_fixes(node_type, parameters=None):
        """Apply automatic fixes to parameters based on best practices."""
        fixed_parameters = parameters.copy() if parameters else {}
        
        # Auto-fix common issues
        if node_type == "Write":
            fixed_parameters["create_directories"] = True
        
        if node_type in ["Grade", "ColorCorrect", "HueCorrect"]:
            # Add default mask channel if not specified
            if "maskChannelInput" not in fixed_parameters:
                fixed_parameters["maskChannelInput"] = "none"
        
        if node_type == "Merge":
            # Set default operation to over if not specified
            if "operation" not in fixed_parameters:
                fixed_parameters["operation"] = "over"
            # Set bbox handling to union if not specified
            if "bbox" not in fixed_parameters:
                fixed_parameters["bbox"] = "union"
                
        if node_type == "Blur":
            # Only blur RGB by default, not alpha
            if "channels" not in fixed_parameters:
                fixed_parameters["channels"] = "rgb"
                
        return fixed_parameters
    
    @staticmethod
    def suggest_node_sequence(task):
        """Suggest a sequence of nodes for common compositing tasks."""
        sequences = {
            "keying": [
                {"type": "Keyer", "name": "Keyer1"},
                {"type": "Unpremult", "name": "Unpremult1"},
                {"type": "Grade", "name": "Despill", "parameters": {"label": "Despill"}},
                {"type": "Premult", "name": "Premult1"},
                {"type": "EdgeBlur", "name": "EdgeBlur1", "parameters": {"size": 2}}
            ],
            "color_correction": [
                {"type": "Unpremult", "name": "Unpremult1"},
                {"type": "Grade", "name": "Grade1", "parameters": {"label": "Overall"}},
                {"type": "Grade", "name": "GradeShadows", "parameters": {"label": "Shadows"}},
                {"type": "Grade", "name": "GradeHighlights", "parameters": {"label": "Highlights"}},
                {"type": "Premult", "name": "Premult1"}
            ],
            "lens_distortion": [
                {"type": "LensDistortion", "name": "Undistort", "parameters": {"label": "Undistort"}},
                # Processing nodes would go here
                {"type": "LensDistortion", "name": "Redistort", "parameters": {"label": "Redistort", "direction": "distort"}}
            ]
        }
        
        return sequences.get(task, [])
    
    @staticmethod
    def suggest_backdrop_organization():
        """Suggest backdrop organization for node graph clarity."""
        return [
            {"name": "INPUTS", "color": "0x7171C6FF"},  # Blue
            {"name": "PREP", "color": "0x9292E1FF"},    # Light Blue
            {"name": "KEY", "color": "0x8A8A5BFF"},     # Olive
            {"name": "COLOR", "color": "0xC67171FF"},   # Red 
            {"name": "FX", "color": "0x71C691FF"},      # Green
            {"name": "OUTPUT", "color": "0xDFDF36FF"}   # Yellow
        ]

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
        
        # Add workflow suggestions for this node type
        suggestions = NukeWorkflowRules.get_node_type_suggestions(node_type)
        if suggestions:
            output += "\nBest practices for this node type:\n"
            for suggestion in suggestions:
                output += f"  - {suggestion}\n"
        
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
    Create a new node in the Nuke script with workflow rule enforcement.
    
    Parameters:
    - node_type: Type of node to create (e.g., "Blur", "Grade", "Merge2")
    - name: Optional name for the new node
    - position: Optional [x, y] position coordinates
    - inputs: Optional list of node names to connect as inputs
    - parameters: Optional dictionary of parameter name/value pairs
    """
    try:
        # Check workflow rules before creation
        issues = NukeWorkflowRules.validate_node_creation(node_type, name, parameters)
        
        # Get suggestions for this node type
        suggestions = NukeWorkflowRules.get_node_type_suggestions(node_type)
        
        # Apply workflow policy - warn or enforce
        warnings = []
        if issues:
            warnings = ["Workflow rules applied:"] + issues
            logger.warning("\n- ".join(warnings))
        
        # Apply auto-fixes to parameters based on best practices
        if parameters is None:
            parameters = {}
        else:
            parameters = NukeWorkflowRules.apply_auto_fixes(node_type, parameters)
        
        # Rule: Add source info in Label for Read nodes
        if node_type == "Read" and "file" in parameters and "label" not in parameters:
            file_path = parameters["file"]
            parameters["label"] = f"Source: {os.path.basename(file_path)}"
        
        # Rule: Add color correction helpers
        color_correction_nodes = ["Grade", "ColorCorrect", "HueCorrect", "Saturation"]
        if node_type in color_correction_nodes and inputs and len(inputs) > 0:
            # Check if the preceding node should have an Unpremult
            input_node = inputs[0]
            logger.info(f"Checking if {input_node} needs unpremult before {node_type}")
            # Logic would go here to check if we need to auto-insert an Unpremult
        
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
        message = f"Created {node_type} node named '{actual_name}'"
        
        # Add warnings about rule suggestions if any
        if warnings:
            message += f"\n\nWorkflow notes:\n- " + "\n- ".join(issues)
        
        # Add node-specific suggestions
        if suggestions:
            message += f"\n\nBest practices for {node_type}:\n- " + "\n- ".join(suggestions)
            
        return message
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
    Modify an existing node in the Nuke script with workflow rule enforcement.
    
    Parameters:
    - name: Name of the node to modify
    - parameters: Optional dictionary of parameter name/value pairs
    - position: Optional [x, y] position coordinates
    - inputs: Optional list of node names to connect as inputs
    """
    try:
        # First, get node info to know its type
        logger.info(f"Getting info for node {name} before modification")
        nuke = get_nuke_connection()
        node_info = None
        try:
            node_info = nuke.send_command("get_node_info", {"name": name})
            node_type = node_info.get("type", "unknown")
        except Exception as e:
            logger.warning(f"Couldn't get node info for {name}: {str(e)}")
            node_type = "unknown"
        
        # Check workflow rules for modification
        issues = NukeWorkflowRules.validate_node_modification(name, parameters, position)
        
        # Position-related rules if position is being changed
        if position:
            position_issues = NukeWorkflowRules.validate_node_position(name, position)
            issues.extend(position_issues)
        
        # Connection-related rules if inputs are being changed
        if inputs:
            for i, input_name in enumerate(inputs):
                if input_name:  # Skip empty connections
                    connection_issues = NukeWorkflowRules.validate_node_connection(input_name, name, i)
                    issues.extend(connection_issues)
        
        # Apply workflow policy - warn or fix issues
        warnings = []
        if issues:
            warnings = ["Workflow rules applied:"] + issues
            logger.warning("\n- ".join(warnings))
            
            # Apply automatic fixes to parameters
            if parameters:
                # Fix: Don't rename, use label instead
                if "name" in parameters:
                    # Move name change to label
                    if "label" not in parameters:
                        parameters["label"] = f"({parameters['name']})"
                    else:
                        parameters["label"] += f" ({parameters['name']})"
                    # Remove the name parameter
                    del parameters["name"]
                    warnings.append("Moved node renaming to label instead")
                
                # Apply type-specific fixes
                if node_type != "unknown":
                    parameters = NukeWorkflowRules.apply_auto_fixes(node_type, parameters)
        
        logger.info(f"Tool called: modify_node for node {name}")
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
        
        message = ""
        if modified_params:
            message = f"Modified node '{name}' - updated: {', '.join(modified_params)}"
        else:
            message = f"Node '{name}' unchanged - no modifications specified"
            
        # Add warnings about rule suggestions if any
        if warnings:
            message += f"\n\nWorkflow notes:\n- " + "\n- ".join(issues)
            
        # Add node-specific suggestions if we know the type
        if node_type != "unknown":
            suggestions = NukeWorkflowRules.get_node_type_suggestions(node_type)
            if suggestions:
                message += f"\n\nBest practices for {node_type}:\n- " + "\n- ".join(suggestions)
            
        return message
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
        
        # Check workflow rules for positioning
        issues = NukeWorkflowRules.validate_node_position(name, [x, y])
        if issues:
            logger.warning(f"Positioning suggestions for {name}:\n- " + "\n- ".join(issues))
        
        result = nuke.send_command("position_node", {
            "name": name,
            "position": [x, y]
        })
        
        message = f"Positioned node '{name}' at X={x}, Y={y}"
        if issues:
            message += "\n\nOrganization tips:\n- " + "\n- ".join(issues)
            
        return message
    except Exception as e:
        logger.error(f"Error in position_node: {str(e)}")
        return f"Error positioning node: {str(e)}"

@mcp.tool()
def connect_nodes(
    ctx: Context, 
    output_node: str, 
    input_node: str, 
    input_index: int = 0
) -> str:
    """
    Connect nodes together in the Nuke script with workflow rule enforcement.
    
    Parameters:
    - output_node: Name of the node whose output to connect
    - input_node: Name of the node to connect the output to
    - input_index: Input index on the receiving node (default: 0)
    """
    try:
        # Check workflow rules for connections
        issues = NukeWorkflowRules.validate_node_connection(output_node, input_node, input_index)
        
        # Apply workflow policy - warn or enforce
        warnings = []
        fixes_applied = False
        
        if issues:
            warnings = ["Connection workflow notes:"] + issues
            logger.warning("\n- ".join(warnings))
            
            # Auto-fix: For Merge nodes, use B input (index 1) for main pipeline
            if "Merge" in input_node and input_index == 0:
                logger.info(f"Auto-fixing: Changing Merge input from A (0) to B (1) for main pipeline")
                input_index = 1
                fixes_applied = True
                warnings.append("Automatically connected to Merge input B (1) instead of A (0) for proper B-pipe structure")
        
        logger.info(f"Tool called: connect_nodes from {output_node} to {input_node} at index {input_index}")
        nuke = get_nuke_connection()
        result = nuke.send_command("connect_nodes", {
            "output_node": output_node,
            "input_node": input_node,
            "input_index": input_index
        })
        
        message = f"Connected output of '{output_node}' to input {input_index} of '{input_node}'"
        
        # If we fixed something automatically, mention it
        if fixes_applied:
            message += " (with automatic workflow fixes)"
        
        # Add warnings about rule suggestions if any
        if warnings:
            message += f"\n\nWorkflow notes:\n- " + "\n- ".join(issues)
            
        return message
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
        
        # Use the fixed auto_layout implementation to avoid the "expecting a Nuke node type" error
        auto_layout_code = """
import nuke

def auto_layout_nodes(selected_only={}):
    # Automatically arrange nodes in the script
    try:
        if selected_only:
            # Get selected nodes
            nodes = [n for n in nuke.allNodes() if n.isSelected()]
            if not nodes:
                return "No nodes selected"
        else:
            # Get all nodes
            nodes = nuke.allNodes()
        
        # Use Nuke's auto placement function for individual nodes
        for node in nodes:
            try:
                # Call autoplace on each individual node
                nuke.autoplace(node)
            except Exception as e:
                print("Warning: could not auto-place node " + node.name())
        
        return "Auto-arranged " + str(len(nodes)) + " nodes"
    except Exception as e:
        return "Auto layout error: " + str(e)

# Execute the function
result = auto_layout_nodes({})
output = {{"status": result}}
        """.format(selected_only, selected_only)
        
        result = nuke.send_command("execute_code", {"code": auto_layout_code})
        
        if result.get("executed", False):
            output = result.get("output", {})
            status = output.get("status", "Nodes arranged")
            return status
        else:
            return "Failed to auto-layout nodes"
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

@mcp.tool()
def create_workflow_template(
    ctx: Context,
    template_type: str,
    root_position: List[int] = [0, 0]
) -> str:
    """
    Create a template node structure for common compositing tasks.
    
    Parameters:
    - template_type: Type of template to create (e.g., "keying", "color_correction", "lens_distortion")
    - root_position: Base position for the template [x, y]
    """
    try:
        # Get the template nodes
        template_nodes = NukeWorkflowRules.suggest_node_sequence(template_type)
        
        if not template_nodes:
            return f"No template found for '{template_type}'. Available templates: keying, color_correction, lens_distortion"
        
        # Create the nodes
        nuke = get_nuke_connection()
        created_nodes = []
        prev_node = None
        
        x, y = root_position
        
        for i, node_info in enumerate(template_nodes):
            node_type = node_info.get("type")
            node_name = node_info.get("name")
            node_params = node_info.get("parameters", {})
            
            # Position nodes in a vertical column
            node_position = [x, y + (i * 80)]
            
            # Connect to previous node if there is one
            inputs = None
            if prev_node:
                inputs = [prev_node]
            
            result = nuke.send_command("create_node", {
                "node_type": node_type,
                "name": node_name,
                "position": node_position,
                "inputs": inputs,
                "parameters": node_params
            })
            
            actual_name = result.get("name", node_name)
            created_nodes.append(actual_name)
            prev_node = actual_name
        
        # Create a backdrop node to group them
        backdrop_text = template_type.upper()
        backdrop_position = [x - 50, root_position[1] - 50]
        backdrop_width = 300
        backdrop_height = (len(template_nodes) * 80) + 100
        
        # Get a nice color for the backdrop based on template type
        backdrop_color = {
            "keying": "0x8A8A5BFF",      # Olive
            "color_correction": "0xC67171FF",  # Red
            "lens_distortion": "0x71C691FF"    # Green
        }.get(template_type, "0x7171C6FF")  # Default blue
        
        nuke.send_command("execute_code", {
            "code": f"""
            import nuke
            backdrop = nuke.nodes.BackdropNode(
                xpos={backdrop_position[0]},
                ypos={backdrop_position[1]},
                bdwidth={backdrop_width},
                bdheight={backdrop_height},
                tile_color={backdrop_color},
                note_font_size=42,
                label="{backdrop_text}"
            )
            """
        })
        
        return f"Created {template_type} workflow template with nodes: {', '.join(created_nodes)}"
    except Exception as e:
        logger.error(f"Error in create_workflow_template: {str(e)}")
        return f"Error creating workflow template: {str(e)}"

@mcp.tool()
def organize_node_graph(
    ctx: Context,
    selected_only: bool = False,
    direction: str = "vertical"
) -> str:
    """
    Auto-organize the node graph in a clean, professional layout.
    
    Parameters:
    - selected_only: Only organize selected nodes if True
    - direction: Layout direction, either "vertical" (top to bottom) or "horizontal" (left to right)
    """
    try:
        logger.info(f"Tool called: organize_node_graph with selected_only={selected_only}, direction={direction}")
        nuke = get_nuke_connection()
        
        # First, get script info to know what nodes we're working with
        script_info = nuke.send_command("get_script_info")
        
        nodes = script_info.get("nodes", [])
        if not nodes:
            return "No nodes found to organize"
        
        # Filter nodes if selected_only is True
        if selected_only:
            nodes = [n for n in nodes if n.get("selected", False)]
            if not nodes:
                return "No selected nodes found to organize"
        
        # Basic organization algorithm in Python code (Nuke will execute this)
        # This is a simplified example - a real organizer would be much more sophisticated
        if direction == "vertical":
            spacing_x = 200
            spacing_y = 80
            organization_code = f"""
import nuke

# Function to determine node category
def get_node_category(node):
    node_type = node.Class()
    if "Read" in node_type:
        return "INPUTS", 0
    elif "Write" in node_type:
        return "OUTPUT", 5
    elif node_type in ["Roto", "RotoPaint", "Crop", "Reformat"]:
        return "PREP", 1
    elif node_type in ["Keyer", "Primatte", "IBKColour", "IBKGizmo"]:
        return "KEY", 2
    elif node_type in ["Grade", "ColorCorrect", "HueCorrect", "ColorLookup"]:
        return "COLOR", 3
    elif node_type in ["Blur", "Glow", "VectorBlur", "ZDefocus"]:
        return "FX", 4
    else:
        return "MISC", 6

# Organize nodes by category
categories = {{}}
for n in nuke.allNodes():
    if {selected_only.__str__().lower()} and not n.isSelected():
        continue
        
    # Skip backdrops for now
    if n.Class() == "BackdropNode":
        continue
        
    category, idx = get_node_category(n)
    if category not in categories:
        categories[category] = []
    categories[category].append(n)

# Position nodes by category
x_start = 0
for cat_idx in range(7):  # 0-6 for our categories
    nodes_in_category = []
    category_name = ""
    
    # Find the category with this index
    for cat, nodes in categories.items():
        if not nodes:
            continue
        cat_name, cat_index = get_node_category(nodes[0])
        if cat_index == cat_idx and nodes:
            nodes_in_category = nodes
            category_name = cat_name
            break
    
    if not nodes_in_category:
        continue
        
    # Position nodes in this category
    y_pos = 0
    for i, node in enumerate(nodes_in_category):
        node.setXpos(x_start)
        node.setYpos(y_pos)
        y_pos += {spacing_y}
    
    # Create a backdrop for this category if there are nodes
    if nodes_in_category:
        backdrop = nuke.nodes.BackdropNode(
            xpos=x_start - 50,
            ypos=-50,
            bdwidth=150,
            bdheight=y_pos + 50,
            label=category_name,
            note_font_size=42
        )
        
        # Set backdrop color based on category
        if category_name == "INPUTS":
            backdrop["tile_color"].setValue(0x7171C6FF)  # Blue
        elif category_name == "PREP":
            backdrop["tile_color"].setValue(0x9292E1FF)  # Light Blue
        elif category_name == "KEY":
            backdrop["tile_color"].setValue(0x8A8A5BFF)  # Olive
        elif category_name == "COLOR":
            backdrop["tile_color"].setValue(0xC67171FF)  # Red
        elif category_name == "FX":
            backdrop["tile_color"].setValue(0x71C691FF)  # Green
        elif category_name == "OUTPUT":
            backdrop["tile_color"].setValue(0xDFDF36FF)  # Yellow
    
    x_start += {spacing_x}

output = {{"status": f"Organized node graph with {direction} flow direction"}}
"""
        else:  # horizontal
            spacing_x = 150
            spacing_y = 300
            # Horizontal organization code would be similar but with x/y flipped
            organization_code = """
import nuke
# Horizontal organization not implemented in this example
output = {"status": "Horizontal organization not implemented in this example"}
"""
        
        # Execute the organization code
        result = nuke.send_command("execute_code", {"code": organization_code})
        
        if result.get("executed", False):
            output = result.get("output", {})
            status = output.get("status", f"Organized node graph with {direction} flow direction")
            return status
        else:
            return "Failed to organize nodes"
    except Exception as e:
        logger.error(f"Error in organize_node_graph: {str(e)}")
        return f"Error organizing node graph: {str(e)}"

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
4. Use `organize_node_graph()` to arrange nodes by category with backdrops

## Workflow Templates
1. Create common node setups with `create_workflow_template(template_type="keying")`
2. Available templates include: "keying", "color_correction", "lens_distortion"

## Rendering and Viewing
1. Control playback with `viewer_playback(action="play")`
2. Render frames with `render(frame_range="1-10", write_node="Write1")`
3. Create viewers with `create_viewer(input_node="NodeName")`

## Compositing Best Practices
1. Maintain B-pipe structure (main pipeline connects to B input of Merge nodes)
2. Use Unpremult before color correction operations
3. Keep node graph organized with a top-to-bottom, left-to-right flow
4. Use labels for descriptions instead of renaming nodes
5. Group related nodes with backdrops
"""

def main():
    """Run the NukeMCP server"""
    logger.info("Starting NukeMCP main function")
    logger.info("This server connects to Nuke and exposes MCP tools")
    logger.info("Make sure Nuke is running with the NukeMCP addon active")
    mcp.run()

if __name__ == "__main__":
    main()