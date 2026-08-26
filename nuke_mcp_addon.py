import nuke
import json
import threading
import socket
import time
import traceback
import os
import re
from typing import Dict, Any, List, Optional, Union

# nukescripts is imported lazily. Importing it at module scope fails while
# init.py runs, because PythonPanel is not registered until Nuke's UI is up.

# Must match NUKE_MCP_HOST/NUKE_MCP_PORT used by the MCP server process.
DEFAULT_HOST = os.getenv("NUKE_MCP_HOST", "localhost")
DEFAULT_PORT = int(os.getenv("NUKE_MCP_PORT", "9876"))

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
SOCKET_POLL_TIMEOUT = 0.1
# Sending a response must not inherit the short read-poll timeout, otherwise
# sendall can be interrupted mid-frame and truncate the newline-framed reply.
RESPONSE_SEND_TIMEOUT = 30.0

_OUTCOME_MARKER = "__nuke_mcp_outcome__"


class MainThreadDispatchError(Exception):
    """Raised when main-thread dispatch produced no usable handler outcome."""


class MainThreadHandlerError(Exception):
    """Carries a handler failure captured on Nuke's main thread."""

    def __init__(self, error_type, message):
        self.error_type = error_type
        self.error_message = message
        super().__init__("%s: %s" % (error_type, message))


def encode_message(payload):
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


_PRIMITIVES = (bool, int, float, str)


def _as_reportable(value):
    """Reduce a knob value to something JSON can encode.

    Returns ``(ok, value)``. Anything that would only encode as an object repr
    is rejected rather than reported, so callers never see ``<Foo object at
    0x...>`` in place of real data.
    """
    if value is None or isinstance(value, _PRIMITIVES):
        return True, value
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if item is not None and not isinstance(item, _PRIMITIVES):
                return False, None
            items.append(item)
        return True, items
    return False, None


def knob_value(knob):
    """Best-effort JSON-safe value for a Nuke knob.

    Multi-component knobs report every component. Nuke exposes a Grade's white
    and gamma as AColor_Knob with four components, and reading only ``value()``
    collapses them to the first channel.
    """
    array_size = getattr(knob, "arraySize", None)
    if callable(array_size):
        try:
            size = array_size()
        except Exception:
            size = None
        if isinstance(size, int) and size > 1:
            try:
                return _as_reportable([knob.value(i) for i in range(size)])
            except Exception:
                pass

    try:
        return _as_reportable(knob.value())
    except Exception:
        return False, None


def format_description(fmt):
    """Describe a Nuke Format. Its __str__ is only an object repr."""
    if fmt is None:
        return None

    try:
        name = fmt.name()
    except Exception:
        name = None

    try:
        dimensions = "%dx%d" % (fmt.width(), fmt.height())
    except Exception:
        dimensions = None

    if name and dimensions:
        return "%s (%s)" % (name, dimensions)
    if name:
        return name
    if dimensions:
        return dimensions
    return str(fmt)


class NukeMCPServer:
    def __init__(self, host=None, port=None):
        host = DEFAULT_HOST if host is None else host
        port = DEFAULT_PORT if port is None else int(port)
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b''
        self.server_thread = None

        # Cache for valid node types
        self._valid_node_types = None

    def start(self):
        """Start the socket server"""
        if self.running:
            return True

        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.settimeout(SOCKET_POLL_TIMEOUT)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            print(f"NukeMCP server started on {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"Failed to start server: {str(e)}")
            self.stop()
            return False

    def stop(self):
        """Stop the socket server"""
        self.running = False
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        self.buffer = b''

        if self.server_thread is not None:
            if threading.current_thread() is not self.server_thread:
                self.server_thread.join(timeout=1.0)
            self.server_thread = None

        print("NukeMCP server stopped")

    def _report_loop_error(self, context, exc):
        """Report a loop socket error, unless stop() caused it.

        Returns True when the error was reported. After ``stop()`` clears
        ``running`` it also closes the sockets, so accept/recv failures are
        the expected consequence of shutdown rather than a real fault.
        """
        if not self.running:
            return False
        print(f"Error {context}: {str(exc)}")
        return True

    def _reset_client(self, client=None):
        """Close a client session and discard its partial buffer."""
        target = client if client is not None else self.client
        if target is not None:
            try:
                target.close()
            except Exception:
                pass
        if client is None or self.client is client:
            self.client = None
        self.buffer = b''

    def _send_responses(self, client, responses):
        """Send framed responses; a failed send ends the client session."""
        try:
            client.settimeout(RESPONSE_SEND_TIMEOUT)
            for response in responses:
                client.sendall(response)
            client.settimeout(SOCKET_POLL_TIMEOUT)
            return True
        except Exception as e:
            print(f"Error sending response, closing client session: {str(e)}")
            self._reset_client(client)
            return False

    def _server_loop(self):
        """Main server loop that runs in a separate thread"""
        while self.running:
            try:
                listen_sock = self.socket
                if listen_sock is None and self.client is None:
                    time.sleep(SOCKET_POLL_TIMEOUT)
                    continue

                if self.client is None and listen_sock is not None:
                    try:
                        client, address = listen_sock.accept()
                        client.settimeout(SOCKET_POLL_TIMEOUT)
                        self.client = client
                        self.buffer = b''
                        print(f"Connected to client: {address}")
                    except socket.timeout:
                        pass
                    except Exception as e:
                        self._report_loop_error("accepting connection", e)

                client = self.client
                if client is None:
                    continue

                try:
                    data = client.recv(8192)
                except socket.timeout:
                    continue
                except Exception as e:
                    self._report_loop_error("receiving data", e)
                    self._reset_client(client)
                    continue

                if not data:
                    print("Client disconnected")
                    self._reset_client(client)
                    continue

                try:
                    responses = self._feed_data(data)
                except Exception as e:
                    print(f"Error processing request data: {str(e)}")
                    traceback.print_exc()
                    self._reset_client(client)
                    continue

                self._send_responses(client, responses)

            except Exception as e:
                print(f"Server error: {str(e)}")

    def _success_response(self, request_id, result):
        return encode_message({
            "id": request_id,
            "status": "success",
            "result": result,
        })

    def _error_response(self, request_id, error_type, message):
        return encode_message({
            "id": request_id,
            "status": "error",
            "error": {"type": error_type, "message": message},
        })

    def _extract_request_id(self, text):
        match = re.search(r'"id"\s*:\s*"([^"]*)"', text)
        if match:
            return match.group(1)
        return None

    def _process_line(self, line):
        """Process a single newline-delimited request line."""
        request_id = None
        if len(line) > MAX_MESSAGE_BYTES:
            return self._error_response(
                request_id,
                "MessageTooLarge",
                "Message exceeds maximum size",
            )

        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as e:
            return self._error_response(
                request_id,
                "UnicodeDecodeError",
                str(e),
            )

        try:
            command = json.loads(text)
        except json.JSONDecodeError as e:
            request_id = self._extract_request_id(text)
            return self._error_response(
                request_id,
                "JSONDecodeError",
                str(e),
            )

        if not isinstance(command, dict):
            return self._error_response(
                None,
                "InvalidRequest",
                "Request must be a JSON object",
            )

        request_id = command.get("id")
        envelope = self.execute_command(command)
        return encode_message(envelope)

    def _feed_data(self, data):
        """Append socket data to the buffer and process complete lines."""
        self.buffer += data
        responses = []

        if len(self.buffer) > MAX_MESSAGE_BYTES and b"\n" not in self.buffer:
            responses.append(self._error_response(
                None,
                "MessageTooLarge",
                "Unterminated message exceeds maximum size",
            ))
            self.buffer = b""
            return responses

        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            responses.append(self._process_line(line))

        if len(self.buffer) > MAX_MESSAGE_BYTES:
            responses.append(self._error_response(
                None,
                "MessageTooLarge",
                "Unterminated message exceeds maximum size",
            ))
            self.buffer = b""

        return responses

    def _run_in_nuke(self, handler, params):
        """Run a handler on Nuke's main thread and recover its outcome.

        The dispatcher is not trusted to propagate exceptions, so the handler
        is wrapped in a guarded callable that returns a tagged outcome.
        """
        dispatcher = getattr(nuke, "executeInMainThreadWithResult", None)
        if dispatcher is None:
            return handler(**params)

        def guarded_handler(**kwargs):
            try:
                return {
                    _OUTCOME_MARKER: True,
                    "ok": True,
                    "value": handler(**kwargs),
                }
            except Exception as exc:
                traceback.print_exc()
                return {
                    _OUTCOME_MARKER: True,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }

        outcome = dispatcher(guarded_handler, kwargs=params)

        if not isinstance(outcome, dict) or outcome.get(_OUTCOME_MARKER) is not True:
            raise MainThreadDispatchError(
                "Nuke main-thread dispatch returned no usable handler outcome; "
                "the command result is unknown and may have partially applied"
            )

        if outcome.get("ok"):
            return outcome.get("value")

        raise MainThreadHandlerError(
            outcome.get("error_type") or "HandlerError",
            outcome.get("message") or "Handler failed on Nuke's main thread",
        )

    def ping(self, **kwargs):
        return {"pong": True}

    def execute_command(self, command):
        """Execute a command received from the client"""
        request_id = command.get("id")
        cmd_type = command.get("type")
        params = command.get("params", {})

        handlers = {
            "ping": self.ping,
            "get_script_info": self.get_script_info,
            "create_node": self.create_node,
            "modify_node": self.modify_node,
            "delete_node": self.delete_node,
            "position_node": self.position_node,
            "connect_nodes": self.connect_nodes,
            "render": self.render,
            "viewer_playback": self.viewer_playback,
            "execute_code": self.execute_code,
            "auto_layout": self.auto_layout,
            "get_node_info": self.get_node_info,
            "set_frames": self.set_frames,
            "create_viewer": self.create_viewer,
        }

        handler = handlers.get(cmd_type)
        if not handler:
            return {
                "id": request_id,
                "status": "error",
                "error": {
                    "type": "UnknownCommand",
                    "message": "Unknown command type: %s" % cmd_type,
                },
            }

        try:
            print("Executing handler for %s" % cmd_type)
            start_time = time.time()

            if cmd_type == "ping":
                result = handler(**params)
            else:
                result = self._run_in_nuke(handler, params)

            end_time = time.time()
            execution_time = end_time - start_time
            print("Handler execution complete in %.2f seconds" % execution_time)

            return {
                "id": request_id,
                "status": "success",
                "result": result,
            }
        except Exception as e:
            print("Error in handler: %s" % str(e))
            traceback.print_exc()
            if isinstance(e, MainThreadHandlerError):
                error_type = e.error_type
                error_message = e.error_message
            else:
                error_type = type(e).__name__
                error_message = str(e)
            return {
                "id": request_id,
                "status": "error",
                "error": {
                    "type": error_type,
                    "message": error_message,
                },
            }
    
    def _get_valid_node_types(self):
        """Return a list of valid Nuke node types to prevent crashes."""
        # Use cached list if available
        if self._valid_node_types is not None:
            return self._valid_node_types
            
        # Standard Nuke node types that should always be available
        standard_node_types = [
            # Input/Output
            "Read", "Write", "Viewer",
            # Color
            "Grade", "ColorCorrect", "Saturation", "HueCorrect", "ColorLookup",
            # Channel
            "Shuffle", "ShuffleCopy", "Copy",
            # Filter
            "Blur", "Defocus", "Sharpen", "Median", "EdgeBlur",
            # Keyer
            "Keyer", "Primatte", "IBKColour", "IBKGizmo", "Keylight",
            # Merge
            "Merge2", "Premult", "Unpremult", "Screen", "Plus",
            # Transform
            "Transform", "Reformat", "Crop", "CornerPin2D",
            # 3D
            "Scene", "Camera", "Light", "Axis", "Card", "Cube", "Sphere", "ScanlineRender",
            # Deep
            "DeepMerge", "DeepRecolor",
            # Misc
            "Dot", "Switch", "TimeOffset", "NoOp", "Text", "Roto", "RotoPaint",
            # Special nodes
            "BackdropNode"
        ]
        
        # Try to get actual node classes from environment if possible
        try:
            all_nodes = set(standard_node_types)
            
            # Add any nodes from menu
            try:
                menu_items = nuke.menu("Nodes").items()
                for item in menu_items:
                    if hasattr(item, 'name'):
                        all_nodes.add(item.name())
            except:
                print("Warning: Could not get node types from menu")
                
            # Store the combined list
            self._valid_node_types = list(all_nodes)
            print(f"Found {len(self._valid_node_types)} valid node types")
            
        except Exception as e:
            # Fallback to standard list
            print(f"Warning: Using fallback node list due to error: {str(e)}")
            self._valid_node_types = standard_node_types
            
        return self._valid_node_types
        
    def _normalize_node_type(self, node_type):
        """
        Convert common incorrect node type names to their proper equivalents.
        Returns the correct node type name or None if no match found.
        """
        # Mapping of incorrect to correct node types
        node_type_corrections = {
            # Common mistakes
            "Output": "Write",
            "WriteNode": "Write",
            "Input": "Read",
            "ReadNode": "Read",
            "Merge": "Merge2",
            "ColorCorrection": "ColorCorrect",
            "Color": "Grade",
            "Grading": "Grade",
            "Gaussian": "Blur",
            "GaussianBlur": "Blur",
            "BlurNode": "Blur",
            "Premultiply": "Premult", 
            "PreMult": "Premult",
            "Unpremultiply": "Unpremult",
            "UnPreMult": "Unpremult",
            "Move": "Transform",
            "Position": "Transform",
            "Rectangle": "Crop",
            "CropNode": "Crop",
        }
        
        # Check for exact match first
        valid_types = self._get_valid_node_types()
        if node_type in valid_types:
            return node_type
            
        # Check for known corrections
        if node_type in node_type_corrections:
            corrected_type = node_type_corrections[node_type]
            print(f"Corrected node type '{node_type}' to '{corrected_type}'")
            return corrected_type
            
        # No correction found
        return None
        
    def _validate_node_name(self, name):
        """
        Validate a node name to prevent problematic naming patterns.
        Returns a safe version of the name.
        """
        if not name:
            return None
        
        # Check if name starts with a number
        if name[0].isdigit():
            print(f"Warning: Node name '{name}' starts with a number, which can cause issues")
            # Prefix with a safe character (n_)
            name = f"n_{name}"
        
        # Replace any invalid characters
        # Nuke node names should only contain alphanumeric and underscore
        import re
        safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        
        if safe_name != name:
            print(f"Warning: Node name '{name}' contained invalid characters, using '{safe_name}' instead")
        
        return safe_name
    
    def get_script_info(self):
        """Get information about the current Nuke script"""
        try:
            # Basic script info
            script_info = {
                "name": nuke.root().name(),
                "fps": nuke.root().fps(),
                "format": format_description(nuke.root().format()),
                "first_frame": nuke.root()["first_frame"].value(),
                "last_frame": nuke.root()["last_frame"].value(),
                "nodes": [],
            }
            
            # Collect information about nodes
            for node in nuke.allNodes():
                node_info = {
                    "name": node.name(),
                    "type": node.Class(),
                    "position": [node.xpos(), node.ypos()],
                    "selected": node.isSelected(),
                }
                script_info["nodes"].append(node_info)
            
            return script_info
        except Exception as e:
            print(f"Error in get_script_info: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to get script info: {str(e)}")
    
    def create_node(self, node_type, name=None, position=None, inputs=None, parameters=None):
        """Create a new node in Nuke with improved stability"""
        try:
            # Default parameters
            if position is None:
                position = [0, 0]
            if parameters is None:
                parameters = {}
            
            # Normalize node type - handle common mistakes
            corrected_node_type = self._normalize_node_type(node_type)
            if corrected_node_type and corrected_node_type != node_type:
                print(f"Automatically corrected node type from '{node_type}' to '{corrected_node_type}'")
                node_type = corrected_node_type
            
            # Validate node type before attempting creation
            valid_node_types = self._get_valid_node_types()
            if node_type not in valid_node_types:
                import difflib
                similar_types = difflib.get_close_matches(node_type, valid_node_types, n=3, cutoff=0.6)
                
                error_msg = f"Invalid node type: '{node_type}'"
                if similar_types:
                    error_msg += f". Did you mean one of these: {', '.join(similar_types)}?"
                else:
                    error_msg += f". Valid types include: {', '.join(sorted(valid_node_types[:10]))}..."
                
                raise ValueError(error_msg)
            
            # Special case for BackdropNode which requires a different creation pattern
            if node_type == "BackdropNode":
                node = nuke.nodes.BackdropNode(
                    xpos=position[0],
                    ypos=position[1]
                )
            else:
                # Standard node creation
                node = nuke.createNode(node_type, inpanel=False)
            
            # Verify node was created
            if not node:
                raise ValueError(f"Failed to create node of type {node_type}")
            
            # Set name if provided
            if name:
                safe_name = self._validate_node_name(name)
                if safe_name:
                    # Check if name already exists
                    existing = nuke.toNode(safe_name)
                    if existing:
                        suffix = 1
                        while nuke.toNode(f"{safe_name}_{suffix}"):
                            suffix += 1
                        safe_name = f"{safe_name}_{suffix}"
                    
                    node.setName(safe_name)
            
            # Set position
            node.setXYpos(position[0], position[1])
            
            # Set parameters - only if they exist, with type conversion
            for param_name, param_value in parameters.items():
                # CRITICAL: Verify the parameter exists before setting
                if param_name in node.knobs():
                    try:
                        # Get knob class to handle type conversion properly
                        knob = node[param_name]
                        knob_class = knob.Class()
                        
                        # Handle different knob types with appropriate type conversion
                        if knob_class in ["Int_Knob", "WH_Knob"] and isinstance(param_value, str):
                            # Try to convert string to int for integer knobs
                            try:
                                node[param_name].setValue(int(param_value))
                                print(f"Converted string '{param_value}' to int for parameter {param_name}")
                            except ValueError:
                                print(f"Warning: Could not convert '{param_value}' to int for {param_name}")
                        elif knob_class in ["Double_Knob", "XY_Knob", "XYZ_Knob", "AColor_Knob", "WH_Knob"] and isinstance(param_value, str):
                            # Try to convert string to float for float knobs
                            try:
                                node[param_name].setValue(float(param_value))
                                print(f"Converted string '{param_value}' to float for parameter {param_name}")
                            except ValueError:
                                print(f"Warning: Could not convert '{param_value}' to float for {param_name}")
                        elif knob_class == "Boolean_Knob" and isinstance(param_value, str):
                            # Convert string to boolean
                            bool_value = param_value.lower() in ["true", "yes", "1", "on"]
                            node[param_name].setValue(bool_value)
                            print(f"Converted string '{param_value}' to boolean {bool_value} for parameter {param_name}")
                        elif knob_class in ["Color_Knob", "AColor_Knob"] and isinstance(param_value, list):
                            # Handle color knobs (list of floats)
                            for i, comp in enumerate(param_value):
                                if i < 4:  # RGBA has max 4 components
                                    if isinstance(comp, str):
                                        try:
                                            node[param_name].setValue(float(comp), i)
                                        except ValueError:
                                            print(f"Warning: Could not convert '{comp}' to float for {param_name}[{i}]")
                                    else:
                                        node[param_name].setValue(comp, i)
                        else:
                            # Default setting
                            node[param_name].setValue(param_value)
                    except Exception as e:
                        print(f"Warning: Error setting parameter {param_name} to {param_value}: {str(e)}")
                        traceback.print_exc()
                        # Continue with other parameters instead of failing
                else:
                    print(f"Warning: Parameter {param_name} does not exist on {node_type}")
            
            # Connect inputs only AFTER all parameters are set
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    if input_name:
                        input_node = nuke.toNode(input_name)
                        if input_node:
                            node.setInput(input_idx, input_node)
                        else:
                            print(f"Warning: Input node {input_name} not found")
            
            # Return node information
            return {
                "name": node.name(),
                "type": node.Class(),
                "position": [node.xpos(), node.ypos()],
            }
        except Exception as e:
            print(f"Error creating node: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to create node: {str(e)}")
    
    def modify_node(self, name, parameters=None, position=None, inputs=None):
        """Modify an existing node"""
        try:
            # Get the node
            node = nuke.toNode(name)
            if not node:
                raise ValueError(f"Node not found: {name}")
            
            # Set position if provided
            if position:
                node.setXYpos(position[0], position[1])
            
            # Set parameters if provided - only if they exist
            if parameters:
                for param_name, param_value in parameters.items():
                    if param_name in node.knobs():
                        node[param_name].setValue(param_value)
                    else:
                        print(f"Warning: Parameter {param_name} does not exist on {node.Class()}")
            
            # Connect inputs if specified - only after other modifications
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    if input_name:
                        input_node = nuke.toNode(input_name)
                        if input_node:
                            node.setInput(input_idx, input_node)
                        else:
                            print(f"Warning: Input node {input_name} not found")
                    else:
                        # Disconnect input if None
                        node.setInput(input_idx, None)
            
            # Return updated node information
            node_info = {
                "name": node.name(),
                "type": node.Class(),
                "position": [node.xpos(), node.ypos()],
                "parameters": {}
            }
            
            # Include some key parameters in response
            for knob in node.knobs():
                try:
                    if node[knob].visible() and not node[knob].isAnimated():
                        value = node[knob].value()
                        # Only include simple parameter types
                        if isinstance(value, (int, float, str, bool)):
                            node_info["parameters"][knob] = value
                except:
                    pass
            
            return node_info
        except Exception as e:
            print(f"Error modifying node: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to modify node: {str(e)}")
            
    def delete_node(self, name):
        """Delete a node"""
        try:
            # Get the node
            node = nuke.toNode(name)
            if not node:
                raise ValueError(f"Node not found: {name}")
            
            # Store the name to return
            node_name = node.name()
            node_type = node.Class()
            
            # Delete the node
            nuke.delete(node)
            
            return {
                "deleted": node_name,
                "type": node_type
            }
        except Exception as e:
            print(f"Error deleting node: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to delete node: {str(e)}")
    
    def position_node(self, name, position):
        """Position a node at specific coordinates"""
        try:
            # Get the node
            node = nuke.toNode(name)
            if not node:
                raise ValueError(f"Node not found: {name}")
            
            # Set position
            node.setXYpos(position[0], position[1])
            
            return {
                "name": node.name(),
                "position": [node.xpos(), node.ypos()]
            }
        except Exception as e:
            print(f"Error positioning node: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to position node: {str(e)}")
    
    def connect_nodes(self, output_node, input_node, input_index=0):
        """Connect nodes together"""
        try:
            # Get the nodes
            out_node = nuke.toNode(output_node)
            in_node = nuke.toNode(input_node)
            
            if not out_node:
                raise ValueError(f"Output node not found: {output_node}")
            if not in_node:
                raise ValueError(f"Input node not found: {input_node}")
            
            # Connect the nodes
            in_node.setInput(input_index, out_node)
            
            return {
                "output_node": output_node,
                "input_node": input_node,
                "input_index": input_index
            }
        except Exception as e:
            print(f"Error connecting nodes: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to connect nodes: {str(e)}")
    
    def render(self, frame_range=None, write_node=None, proxy_mode=False):
        """Render frames from the Nuke script"""
        try:
            # Set proxy mode if requested
            if proxy_mode:
                nuke.setProxy(True)
            else:
                nuke.setProxy(False)
            
            # Process frame range
            if frame_range:
                # Parse frame range string to get the frames to render
                frames = self._parse_frame_range(frame_range)
                if not frames:
                    raise ValueError(f"Invalid frame range: {frame_range}")
                    
                start_frame = frames[0]
                end_frame = frames[-1]
            else:
                # Use script's frame range
                start_frame = int(nuke.root()['first_frame'].value())
                end_frame = int(nuke.root()['last_frame'].value())
            
            # Render specific Write node or all
            if write_node:
                node = nuke.toNode(write_node)
                if not node:
                    raise ValueError(f"Write node not found: {write_node}")
                
                # Check if it's a Write node
                if node.Class() != "Write":
                    raise ValueError(f"Node {write_node} is not a Write node")
                
                # Execute the render
                nuke.execute(node, start_frame, end_frame)
                return {
                    "status": f"Rendered {write_node} for frames {start_frame}-{end_frame}",
                    "frames": [start_frame, end_frame]
                }
            else:
                # Render all Write nodes
                write_nodes = nuke.allNodes('Write')
                if not write_nodes:
                    raise ValueError("No Write nodes found in script")
                
                nuke.executeMultiple(write_nodes, [[start_frame, end_frame]])
                
                # Return the names of the Write nodes that were rendered
                rendered_nodes = [node.name() for node in write_nodes]
                return {
                    "status": f"Rendered {len(rendered_nodes)} Write nodes for frames {start_frame}-{end_frame}",
                    "write_nodes": rendered_nodes,
                    "frames": [start_frame, end_frame]
                }
            
        except Exception as e:
            print(f"Render error: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Render error: {str(e)}")
    
    def _parse_frame_range(self, frame_range_str):
        """Parse a frame range string like '1-5,7,9-12'"""
        frames = []
        try:
            parts = frame_range_str.split(',')
            
            for part in parts:
                if '-' in part:
                    # Range of frames
                    start, end = map(int, part.split('-'))
                    frames.extend(range(start, end + 1))
                else:
                    # Single frame
                    frames.append(int(part))
        except ValueError as e:
            print(f"Error parsing frame range: {str(e)}")
        
        return sorted(frames)
    
    def viewer_playback(self, action="play", start_frame=None, end_frame=None, viewer_index=1):
        """Control Nuke's Viewer playback"""
        try:
            # Get the viewer
            viewer = nuke.activeViewer()
            if not viewer:
                raise ValueError("No active viewer")
            
            # Set frame range if specified
            if start_frame is not None and end_frame is not None:
                viewer.frameRange(start_frame, end_frame)
            
            # Execute requested action
            if action == "play":
                viewer.play()
                return {"status": "Playing in viewer"}
            elif action == "stop":
                viewer.stop()
                return {"status": "Stopped playback"}
            elif action == "next":
                nuke.frame(nuke.frame() + 1)
                return {"status": f"Advanced to frame {nuke.frame()}"}
            elif action == "prev":
                nuke.frame(nuke.frame() - 1)
                return {"status": f"Moved back to frame {nuke.frame()}"}
            else:
                raise ValueError(f"Unknown playback action: {action}")
        
        except Exception as e:
            print(f"Viewer playback error: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Viewer playback error: {str(e)}")
    
    def execute_code(self, code):
        """Execute arbitrary Python code in Nuke with safety measures"""
        try:
            if not code.strip():
                return {"executed": False, "error": "Empty code provided"}
            
            # Create a dictionary to capture output
            output = {}
            
            # Create a local namespace for execution with safety
            import nukescripts

            namespace = {"nuke": nuke, "nukescripts": nukescripts, "output": output}
            
            # Execute the code with safety wrapper
            try:
                exec(code, namespace)
                return {"executed": True, "output": output}
            except Exception as e:
                error_tb = traceback.format_exc()
                print(f"Code execution error: {str(e)}")
                print(error_tb)
                return {"executed": False, "error": str(e), "traceback": error_tb}
        except Exception as e:
            print(f"Error in execute_code setup: {str(e)}")
            traceback.print_exc()
            return {"executed": False, "error": str(e)}
    
    def auto_layout(self, selected_only=False):
        """Automatically arrange nodes in the script"""
        try:
            # Improved auto_layout implementation that avoids the "expecting a Nuke node type" error
            auto_layout_code = f"""
import nuke

def auto_layout_nodes(selected_only={selected_only}):
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
                print(f"Warning: could not auto-place node {{node.name()}}: {{str(e)}}")
        
        return f"Auto-arranged {{len(nodes)}} nodes"
    except Exception as e:
        return f"Auto layout error: {{str(e)}}"

# Execute the function
result = auto_layout_nodes()
output["status"] = result
            """
            
            # Execute the code
            result = self.execute_code(auto_layout_code)
            
            if result.get("executed", False):
                output = result.get("output", {})
                status = output.get("status", "Nodes arranged")
                return {"status": status}
            else:
                error = result.get("error", "Unknown error")
                raise Exception(f"Failed to auto-layout nodes: {error}")
        except Exception as e:
            print(f"Auto layout error: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Auto layout error: {str(e)}")
    
    def get_node_info(self, name):
        """Get detailed information about a specific node"""
        try:
            # Get the node
            node = nuke.toNode(name)
            if not node:
                raise ValueError(f"Node not found: {name}")
            
            # Collect node information
            node_info = {
                "name": node.name(),
                "type": node.Class(),
                "position": [node.xpos(), node.ypos()],
                "selected": node.isSelected(),
                "inputs": [],
                "parameters": {}
            }
            
            # Get inputs
            for i in range(node.inputs()):
                input_node = node.input(i)
                if input_node:
                    node_info["inputs"].append({
                        "index": i,
                        "name": input_node.name(),
                        "type": input_node.Class()
                    })
                else:
                    node_info["inputs"].append(None)
            
            # Get parameters (knobs). Values are read generically rather than by
            # whitelisting knob classes, which used to drop every colour knob.
            for knob in node.knobs():
                try:
                    k = node[knob]
                    if not k.visible():
                        continue

                    ok, value = knob_value(k)
                    if ok:
                        node_info["parameters"][knob] = {
                            "value": value,
                            "type": k.Class(),
                        }
                except Exception as e:
                    print(f"Error getting parameter {knob}: {str(e)}")
            
            return node_info
        except Exception as e:
            print(f"Error getting node info: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to get node info: {str(e)}")
    
    def set_frames(self, first_frame=None, last_frame=None, current_frame=None):
        """Set frame range and current frame"""
        try:
            # Update frame range if specified
            if first_frame is not None:
                nuke.root()["first_frame"].setValue(first_frame)
            
            if last_frame is not None:
                nuke.root()["last_frame"].setValue(last_frame)
            
            # Update current frame if specified
            if current_frame is not None:
                nuke.frame(current_frame)
            
            # Return current settings
            return {
                "first_frame": nuke.root()["first_frame"].value(),
                "last_frame": nuke.root()["last_frame"].value(),
                "current_frame": nuke.frame()
            }
        except Exception as e:
            print(f"Error setting frames: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to set frames: {str(e)}")
    
    def create_viewer(self, input_node=None):
        """Create a Viewer node connected to the specified input node"""
        try:
            # Create the Viewer node - Viewer nodes should use nuke.nodes.Viewer() for stability
            try:
                viewer = nuke.nodes.Viewer()
                
                # Set position to a default offscreen value to avoid overlapping with other nodes
                viewer.setXYpos(0, -300)
            except Exception as e:
                print(f"Error creating Viewer using nuke.nodes.Viewer(): {str(e)}")
                # Fallback to regular createNode method
                viewer = nuke.createNode("Viewer", inpanel=False)
            
            # Connect to input node if specified
            if input_node:
                input_n = nuke.toNode(input_node)
                if not input_n:
                    raise ValueError(f"Input node not found: {input_node}")
                
                viewer.setInput(0, input_n)
            
            return {
                "name": viewer.name(),
                "position": [viewer.xpos(), viewer.ypos()],
                "connected_to": input_node
            }
        except Exception as e:
            print(f"Error creating viewer: {str(e)}")
            traceback.print_exc()
            raise Exception(f"Failed to create viewer: {str(e)}")

# Global instance of the panel and of the server it (or menu.py) started
_panel = None
_global_server = None


def _python_panel_base():
    """Resolve PythonPanel after Nuke has finished starting up."""
    import nukescripts as ns

    base = getattr(ns, "PythonPanel", None)
    if base is None:
        # Some builds expose it under nukescripts.panels
        try:
            from nukescripts import panels as _panels

            base = getattr(_panels, "PythonPanel", None)
        except Exception:
            base = None
    if base is None:
        raise AttributeError(
            "nukescripts.PythonPanel is unavailable. Import nuke_mcp_addon from "
            "menu.py (not init.py), after Nuke has finished starting."
        )
    return base


def _make_panel_class():
    PythonPanel = _python_panel_base()

    class NukeMCPPanel(PythonPanel):
        def __init__(self):
            PythonPanel.__init__(self, 'Nuke MCP', 'com.example.NukeMCP')

            self.port = nuke.Int_Knob('port', 'Port:')
            self.port.setValue(DEFAULT_PORT)
            self.addKnob(self.port)

            self.status = nuke.Text_Knob('status', 'Status:')
            self.status.setValue('Not connected')
            self.addKnob(self.status)

            self.divider = nuke.Text_Knob('divider', '')
            self.addKnob(self.divider)

            self.start_button = nuke.PyScript_Knob('start', 'Start Server')
            self.start_button.setFlag(nuke.STARTLINE)
            self.addKnob(self.start_button)

            self.stop_button = nuke.PyScript_Knob('stop', 'Stop Server')
            self.stop_button.setEnabled(False)
            self.addKnob(self.stop_button)

            self.server = None

        def knobChanged(self, knob):
            """Handle knob changes"""
            if knob == self.start_button:
                self._start_server()
            elif knob == self.stop_button:
                self._stop_server()

        def _start_server(self):
            """Start the MCP server"""
            global _global_server
            if self.server is None:
                port = int(self.port.value())
                self.server = NukeMCPServer(port=port)

                if self.server.start():
                    _global_server = self.server
                    self.status.setValue(f'Running on port {port}')
                    self.start_button.setEnabled(False)
                    self.stop_button.setEnabled(True)
                    self.port.setEnabled(False)
                else:
                    self.status.setValue('Failed to start server')
                    self.server = None

        def _stop_server(self):
            """Stop the MCP server"""
            global _global_server
            if self.server:
                self.server.stop()
                if _global_server is self.server:
                    _global_server = None
                self.server = None

                self.status.setValue('Not connected')
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                self.port.setEnabled(True)

    return NukeMCPPanel


def show_panel():
    """Show the NukeMCP panel (creates the class on first use)."""
    global _panel
    if _panel is None:
        _panel = _make_panel_class()()
    _panel.show()


def ensure_server_running(port=None):
    """Start the socket server without needing the panel UI.

    menu.py calls this so an MCP client can connect to a freshly launched Nuke
    without an operator opening the panel first.
    """
    global _global_server
    if _global_server is not None and _global_server.running:
        print(f"[NukeMCP] already running on port {_global_server.port}")
        return _global_server

    server = NukeMCPServer(port=port)
    if not server.start():
        raise RuntimeError(f"NukeMCP failed to bind port {server.port}")
    _global_server = server
    return server
