import nuke
import nukescripts
import json
import threading
import socket
import time
import traceback
import sys
import io
import math
import random
from typing import Dict, Any, List, Optional, Union

class NukeMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b''  # Buffer for incomplete data
    
    def start(self):
        """Start the socket server"""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)
            
            # Start the server loop in a separate thread
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
        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()
        self.socket = None
        self.client = None
        print("NukeMCP server stopped")

    def _server_loop(self):
        """Main server loop that runs in a separate thread with improved error handling"""
        while self.running:
            try:
                # Accept new connections
                if not self.client and self.socket:
                    try:
                        self.client, address = self.socket.accept()
                        self.client.setblocking(False)
                        print(f"Connected to client: {address}")
                    except BlockingIOError:
                        pass  # No connection waiting
                    except Exception as e:
                        print(f"Error accepting connection: {str(e)}")
                
                # Process existing connection
                if self.client:
                    try:
                        # Try to receive data
                        try:
                            data = self.client.recv(8192)
                            if data:
                                self.buffer += data
                                # Try to process complete messages
                                try:
                                    # Check if we have a complete JSON object
                                    # Look for a complete JSON message by checking for balanced braces
                                    buffer_str = self.buffer.decode('utf-8', errors='replace')
                                    
                                    # Simple check for JSON completeness - might need improvement for complex messages
                                    if buffer_str.count('{') == buffer_str.count('}') and buffer_str.strip().startswith('{'):
                                        try:
                                            # Attempt to parse the buffer as JSON
                                            command = json.loads(buffer_str)
                                            # If successful, clear the buffer and process command
                                            self.buffer = b''
                                            response = self.execute_command(command)
                                            response_json = json.dumps(response)
                                            self.client.sendall(response_json.encode('utf-8'))
                                        except json.JSONDecodeError as e:
                                            # Only consider it an error if we have a lot of data
                                            if len(buffer_str) > 10000:
                                                print(f"JSON decode error with large buffer: {str(e)}")
                                                # Reset buffer if it's gotten too large
                                                self.buffer = b''
                                            # Otherwise, wait for more data
                                            pass
                                except Exception as e:
                                    print(f"Error processing message: {str(e)}")
                                    import traceback
                                    print(traceback.format_exc())
                                    # Clear buffer on error to avoid getting stuck
                                    self.buffer = b''
                            else:
                                # Connection closed by client
                                print("Client disconnected")
                                self.client.close()
                                self.client = None
                                self.buffer = b''
                        except BlockingIOError:
                            pass  # No data available
                        except Exception as e:
                            print(f"Error receiving data: {str(e)}")
                            self.client.close()
                            self.client = None
                            self.buffer = b''
                            
                    except Exception as e:
                        print(f"Error with client: {str(e)}")
                        if self.client:
                            self.client.close()
                            self.client = None
                        self.buffer = b''
                        
            except Exception as e:
                print(f"Server error: {str(e)}")
                import traceback
                print(traceback.format_exc())
            
            # Sleep to prevent CPU hogging
            time.sleep(0.1)

    def execute_command(self, command):
        """Execute a command received from the client with improved error handling"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            
            # Define handlers for different command types
            handlers = {
                "get_script_info": self.get_script_info,
                "create_node": self.create_node,
                "modify_node": self.modify_node,
                "position_node": self.position_node,
                "connect_nodes": self.connect_nodes,
                "render": self.render,
                "viewer_playback": self.viewer_playback,
                "execute_code": self.execute_code,
                "auto_layout": self.auto_layout,
                "get_node_info": self.get_node_info,
                "set_frames": self.set_frames,
                "create_viewer": self.create_viewer
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    print(f"Executing handler for {cmd_type}")
                    
                    # Set up timing to monitor long-running operations
                    import time
                    start_time = time.time()
                    
                    # Execute the handler
                    result = handler(**params)
                    
                    # Log execution time for performance monitoring
                    elapsed_time = time.time() - start_time
                    print(f"Handler execution complete in {elapsed_time:.2f} seconds")
                    
                    return {"status": "success", "result": result}
                except Exception as e:
                    import traceback
                    error_tb = traceback.format_exc()
                    print(f"Error in handler: {str(e)}")
                    print(error_tb)
                    return {
                        "status": "error", 
                        "message": str(e),
                        "traceback": error_tb
                    }
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        except Exception as e:
            import traceback
            error_tb = traceback.format_exc()
            print(f"Error executing command: {str(e)}")
            print(error_tb)
            return {
                "status": "error", 
                "message": str(e),
                "traceback": error_tb
            }
            
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
                "format": str(nuke.root().format()),
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
            return {"error": str(e)}
            
    def _get_valid_node_types(self):
        """Return a list of valid Nuke node types with fallback options."""
        # Cache this list to avoid regenerating it each time
        if not hasattr(self, '_valid_node_types'):
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
                "Scene", "Camera", "Light", "Axis",
                # Deep
                "DeepMerge", "DeepRecolor",
                # Misc
                "Dot", "Switch", "TimeOffset", "NoOp", "Text", "Roto", "RotoPaint"
            ]
            
            # Get additional node types from the environment if possible
            try:
                # Method 1: Get standard node types from the menu 
                menu_nodes = set()
                try:
                    for menu_item in nuke.menu("Nodes").items():
                        if hasattr(menu_item, 'name'):
                            menu_nodes.add(menu_item.name())
                except:
                    print("Warning: Could not get node types from menu")
                
                # Method 2: Get node types from the nuke.nodes module
                code_nodes = set()
                try:
                    for name in dir(nuke.nodes):
                        if not name.startswith('_'):
                            code_nodes.add(name)
                except:
                    print("Warning: Could not get node types from nuke.nodes")
                
                # Combine all sources with the standard list having priority
                all_nodes = set(standard_node_types).union(menu_nodes).union(code_nodes)
                self._valid_node_types = list(all_nodes)
                
                # Debug info
                print(f"Found {len(self._valid_node_types)} valid node types")
            except Exception as e:
                # Fallback to standard nodes if there's any error
                print(f"Warning: Using fallback node list due to error: {str(e)}")
                self._valid_node_types = standard_node_types
        
        return self._valid_node_types
    
    def create_node(self, node_type, name=None, position=None, inputs=None, parameters=None):
        """Create a new node in Nuke with improved error handling and type validation"""
        try:
            # Default parameters
            if position is None:
                position = [0, 0]
            # Ensure position is a list, not a tuple
            elif isinstance(position, tuple):
                position = list(position)
                
            if parameters is None:
                parameters = {}
            
            # Enhanced node type validation
            valid_node_types = self._get_valid_node_types()
            
            if node_type not in valid_node_types:
                # Provide helpful suggestions for similar node types
                import difflib
                similar_types = difflib.get_close_matches(node_type, valid_node_types, n=5, cutoff=0.6)
                
                error_msg = f"Invalid node type: '{node_type}'"
                if similar_types:
                    error_msg += f". Did you mean one of these: {', '.join(similar_types)}?"
                else:
                    error_msg += f". Valid types include: {', '.join(sorted(valid_node_types[:10]))}..."
                    
                raise ValueError(error_msg)
            
            print(f"Creating node of type {node_type}")
            
            # Create the node using safer createNode method
            try:
                node = nuke.createNode(node_type, inpanel=False)
            except Exception as e:
                print(f"Failed to create node: {str(e)}")
                import traceback
                print(traceback.format_exc())
                raise ValueError(f"Could not create node of type {node_type}: {str(e)}")
            
            if not node:
                raise ValueError(f"Failed to create node of type {node_type}")
                
            print(f"Node created successfully: {node.name()}")
            
            # Set name if provided
            if name:
                try:
                    # Validate the name
                    safe_name = self._validate_node_name(name)
                    
                    # Only try to set name if it's valid
                    if safe_name:
                        existing = nuke.toNode(safe_name)
                        if existing:
                            suffix = 1
                            while nuke.toNode(f"{safe_name}_{suffix}"):
                                suffix += 1
                            safe_name = f"{safe_name}_{suffix}"
                        
                        node.setName(safe_name)
                        print(f"Set node name to: {safe_name}")
                        
                        # If name was modified, add original as label
                        if safe_name != name and "label" not in parameters:
                            try:
                                node["label"].setValue(name)
                                print(f"Set node label to original name: {name}")
                            except Exception as e:
                                print(f"Could not set label to original name: {str(e)}")
                except Exception as e:
                    print(f"Warning: Could not set node name to {name}: {str(e)}")
            
            # Set position with error handling
            try:
                # Ensure position is a list of two integers
                x_pos = int(position[0]) if position and len(position) > 0 else 0
                y_pos = int(position[1]) if position and len(position) > 1 else 0
                node.setXYpos(x_pos, y_pos)
                print(f"Set node position to: [{x_pos}, {y_pos}]")
            except Exception as e:
                print(f"Warning: Could not set position to {position}: {str(e)}")
            
            # Set parameters with individual error handling for each parameter
            for param_name, param_value in parameters.items():
                try:
                    if param_name not in node.knobs():
                        print(f"Warning: Parameter {param_name} does not exist on {node_type} node")
                        continue
                        
                    node[param_name].setValue(param_value)
                    print(f"Set parameter {param_name} = {param_value}")
                except Exception as e:
                    print(f"Warning: Error setting parameter {param_name}: {str(e)}")
            
            # Connect inputs if specified, with error handling for each connection
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    if input_name:
                        try:
                            input_node = nuke.toNode(input_name)
                            if input_node:
                                node.setInput(input_idx, input_node)
                                print(f"Connected input {input_idx} to {input_name}")
                            else:
                                print(f"Warning: Could not find input node {input_name}")
                        except Exception as e:
                            print(f"Warning: Error connecting input {input_idx} to {input_name}: {str(e)}")
            
            # Return node information
            return {
                "name": node.name(),
                "type": node.Class(),
                "position": [node.xpos(), node.ypos()],
            }
        except Exception as e:
            import traceback
            error_traceback = traceback.format_exc()
            print(f"Error in create_node: {str(e)}")
            print(f"Traceback: {error_traceback}")
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
                try:
                    node.setXYpos(position[0], position[1])
                    print(f"Set position of {name} to {position}")
                except Exception as e:
                    print(f"Error setting position: {str(e)}")
            
            # Set parameters if provided
            if parameters:
                for param_name, param_value in parameters.items():
                    try:
                        if param_name not in node.knobs():
                            print(f"Warning: Parameter {param_name} does not exist on {node.Class()} node")
                            continue
                            
                        node[param_name].setValue(param_value)
                        print(f"Set parameter {param_name} = {param_value} on {name}")
                    except Exception as e:
                        print(f"Error setting parameter {param_name}: {str(e)}")
            
            # Connect inputs if specified
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    try:
                        if input_name:
                            input_node = nuke.toNode(input_name)
                            if input_node:
                                node.setInput(input_idx, input_node)
                                print(f"Connected input {input_idx} of {name} to {input_name}")
                            else:
                                print(f"Warning: Could not find input node {input_name}")
                        else:
                            # Disconnect input if None
                            node.setInput(input_idx, None)
                            print(f"Disconnected input {input_idx} of {name}")
                    except Exception as e:
                        print(f"Error connecting input {input_idx}: {str(e)}")
            
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
                except Exception as e:
                    print(f"Error getting parameter {knob}: {str(e)}")
            
            return node_info
        except Exception as e:
            import traceback
            error_tb = traceback.format_exc()
            print(f"Failed to modify node: {str(e)}")
            print(error_tb)
            raise Exception(f"Failed to modify node: {str(e)}")
    
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
            raise Exception(f"Render error: {str(e)}")
    
    def _parse_frame_range(self, frame_range_str):
        """Parse a frame range string like '1-5,7,9-12'"""
        frames = []
        parts = frame_range_str.split(',')
        
        for part in parts:
            if '-' in part:
                # Range of frames
                start, end = map(int, part.split('-'))
                frames.extend(range(start, end + 1))
            else:
                # Single frame
                frames.append(int(part))
                
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
            raise Exception(f"Viewer playback error: {str(e)}")
    
    def execute_code(self, code=""):
        """Execute Python code in Nuke with comprehensive safety measures and error handling"""
        # Generate a unique ID for this execution
        import time
        import random
        import traceback  # Import traceback at the method beginning
        execution_id = f"exec_{int(time.time())}_{random.randint(1000, 9999)}"
        
        try:
            print(f"[{execution_id}] Executing Python code: {len(code)} characters")
            
            # 1. Code validation/sanitization
            if not code.strip():
                return {"executed": False, "error": "Empty code provided"}
            
            # 2. Check for potentially dangerous operations
            dangerous_patterns = [
                "shutil.rmtree", "os.rmdir", "os.remove",  # File deletion
                "sys.exit", "os._exit", "quit",  # Program termination
                "subprocess.call", "subprocess.Popen", "os.system",  # Command execution
                "socket.socket", "urllib",  # Network operations
                "exec(", "eval("  # Dynamic code execution
            ]
            
            for pattern in dangerous_patterns:
                if pattern in code:
                    warning_msg = f"Warning: Code contains potentially unsafe operation: {pattern}"
                    print(f"[{execution_id}] {warning_msg}")
                    # Don't block execution, but log the warning
            
            # 3. Pre-validate code for common Nuke errors
            import re
            
            # Get list of valid node types
            if hasattr(self, '_get_valid_node_types'):
                try:
                    valid_node_types = self._get_valid_node_types()
                    
                    # Check for node creation patterns
                    node_creation_patterns = [
                        r'nuke\.createNode\s*\(\s*["\']([^"\']+)["\']',  # nuke.createNode("NodeType")
                        r'nuke\.nodes\.([A-Za-z0-9_]+)\s*\(',            # nuke.nodes.NodeType()
                    ]
                    
                    warnings = []
                    for pattern in node_creation_patterns:
                        matches = re.finditer(pattern, code)
                        for match in matches:
                            node_type = match.group(1)
                            if node_type not in valid_node_types:
                                # Find similar node types for suggestions
                                import difflib
                                similar_types = difflib.get_close_matches(node_type, valid_node_types, n=3, cutoff=0.6)
                                
                                warning = f"Warning: Potentially invalid node type '{node_type}'"
                                if similar_types:
                                    warning += f". Did you mean: {', '.join(similar_types)}?"
                                warnings.append(warning)
                    
                    # Check for risky node parameter access
                    risky_params = [
                        r'node\[["\']([^"\']+)["\']\]\.setValue',  # Setting parameters directly
                    ]
                    
                    for pattern in risky_params:
                        matches = re.finditer(pattern, code)
                        for match in matches:
                            param_name = match.group(1)
                            if param_name not in ["label", "name", "hide_input", "tile_color", 
                                                 "gl_color", "note_font_size", "selected"]:
                                warning = f"Warning: Setting parameter '{param_name}' without existence check"
                                warnings.append(warning)
                    
                    # Check for diagonal node positioning
                    if re.search(r'x\s*\+=.*y\s*\+=', code) or re.search(r'setXYpos\s*\([^,)]*\+[^,)]*,\s*[^,)]*\+', code):
                        warning = "Warning: Detected diagonal node positioning pattern, consider vertical stacking for cleaner graphs"
                        warnings.append(warning)
                    
                    # Log all warnings
                    if warnings:
                        print("Pre-execution code analysis warnings:")
                        for warning in warnings:
                            print(f"  - {warning}")
                except Exception as e:
                    print(f"Warning: Error during code pre-validation: {str(e)}")
            
            # 4. Memory usage tracking (cross-platform version)
            has_resource = False
            initial_memory = 0
            try:
                # Try to use resource module (Unix/Mac)
                import resource
                initial_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                has_resource = True
            except ImportError:
                # Fall back to simple tracking on Windows
                has_resource = False
                print("Resource module not available (Windows) - memory tracking disabled")
            
            # 5. Import any modules needed for the namespace
            import os
            import sys
            import math
            import random
            # Add textwrap module for code indentation
            import textwrap
            
            # 6. Create a namespace with access to nuke and standard modules
            namespace = {
                'nuke': nuke,
                'nukescripts': nukescripts,
                'os': os,
                'sys': sys,
                'math': math,
                'random': random,
                'time': time,
                'traceback': traceback,  # Now traceback is defined
                'output': {},  # Container for output data
                'execution_id': execution_id  # Make ID available to code
            }
            
            # 7. Add print output capturing
            old_stdout = sys.stdout
            string_io = io.StringIO()
            sys.stdout = string_io
            
            try:
                # 8. Safer code execution with prepended safety measures
                # Wrap the code in a function to catch returns and limit scope
                wrapped_code = f"""
    # Safety wrapper for execution {execution_id}
    def __execute_with_safety():
        try:
            # User code begins
    {textwrap.indent(code, '        ')}
            # User code ends
        except Exception as e:
            import traceback
            print(f"[{execution_id}] Error in execution: {{str(e)}}")
            print(traceback.format_exc())
            output['error'] = str(e)
            output['traceback'] = traceback.format_exc()
            return False
        return True

    # Execute the wrapped code
    __execution_success = __execute_with_safety()
    output['success'] = __execution_success
    """
                # Execute the safer wrapped code
                exec(wrapped_code, namespace)
                
                # Capture stdout
                namespace['output']['stdout'] = string_io.getvalue()
                
                # Check memory usage (cross-platform)
                if has_resource:
                    try:
                        final_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        memory_diff = final_memory - initial_memory
                        namespace['output']['memory_usage'] = {
                            'initial_kb': initial_memory,
                            'final_kb': final_memory,
                            'diff_kb': memory_diff
                        }
                        
                        # Check for excessive memory usage
                        if memory_diff > 1024 * 100:  # 100 MB increase
                            print(f"[{execution_id}] Warning: High memory usage detected: {memory_diff/1024:.2f} MB")
                            namespace['output']['high_memory_warning'] = True
                    except Exception as e:
                        print(f"Error tracking memory: {str(e)}")
                else:
                    # Skip memory tracking on Windows
                    namespace['output']['memory_usage'] = {
                        'note': 'Memory tracking not available on Windows'
                    }
                
                # Return successful execution with comprehensive output
                return {"executed": True, "output": namespace.get('output', {})}
                
            except Exception as e:
                # Log and return the error with traceback
                error_msg = f"Code execution error: {str(e)}"
                error_traceback = traceback.format_exc()
                print(f"[{execution_id}] {error_msg}")
                print(error_traceback)
                
                return {
                    "executed": False, 
                    "error": error_msg,
                    "traceback": error_traceback
                }
                
            finally:
                # Always restore stdout
                sys.stdout = old_stdout
                
        except Exception as e:
            # Catch any other errors during setup/teardown
            error_traceback = traceback.format_exc()
            print(f"[{execution_id}] Error in execute_code setup: {str(e)}")
            print(error_traceback)
            return {
                "executed": False, 
                "error": f"Error in execute_code setup: {str(e)}",
                "traceback": error_traceback
            }
        
    def auto_layout(self, selected_only=False):
        """Automatically arrange nodes in the script"""
        try:
            if selected_only:
                # Get selected nodes
                nodes = [n for n in nuke.allNodes() if n.isSelected()]
                if not nodes:
                    return {"status": "No nodes selected"}
            else:
                # Get all nodes
                nodes = nuke.allNodes()
            
            # Use Nuke's auto placement function for individual nodes
            for node in nodes:
                try:
                    # Call autoplace on each individual node
                    nuke.autoplace(node)
                except Exception as e:
                    print(f"Warning: could not auto-place node {node.name()}: {str(e)}")
            
            return {
                "status": f"Auto-arranged {len(nodes)} nodes",
                "selected_only": selected_only
            }
        except Exception as e:
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
            
            # Get parameters (knobs)
            for knob in node.knobs():
                try:
                    if node[knob].visible():
                        k = node[knob]
                        
                        # Get the value based on knob type
                        value = None
                        if k.Class() in ["Int_Knob", "Double_Knob", "Boolean_Knob", "String_Knob"]:
                            value = k.value()
                        elif k.Class() == "XY_Knob":
                            value = [k.value(0), k.value(1)]
                        elif k.Class() == "XYZ_Knob":
                            value = [k.value(0), k.value(1), k.value(2)]
                        elif k.Class() == "Color_Knob":
                            value = [k.value(0), k.value(1), k.value(2), k.value(3)]
                        
                        # Only include parameter if we could get a value
                        if value is not None:
                            node_info["parameters"][knob] = {
                                "value": value,
                                "type": k.Class()
                            }
                except:
                    pass
            
            return node_info
        except Exception as e:
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
            raise Exception(f"Failed to set frames: {str(e)}")
    
    def create_viewer(self, input_node=None):
        """Create a Viewer node connected to the specified input node"""
        try:
            # Create the Viewer node
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
            raise Exception(f"Failed to create viewer: {str(e)}")

from nukescripts.panels import PythonPanel

class NukeMCPPanel(PythonPanel):
    def __init__(self):
        PythonPanel.__init__(self, 'Nuke MCP', 'com.example.NukeMCP')
        
        # Add port field
        self.port = nuke.Int_Knob('port', 'Port:')
        self.port.setValue(9876)
        self.addKnob(self.port)
        
        # Add status field
        self.status = nuke.Text_Knob('status', 'Status:')
        self.status.setValue('Not connected')
        self.addKnob(self.status)
        
        # Add divider
        self.divider = nuke.Text_Knob('divider', '')
        self.addKnob(self.divider)
        
        # Add start button
        self.start_button = nuke.PyScript_Knob('start', 'Start Server')
        self.start_button.setFlag(nuke.STARTLINE)
        self.addKnob(self.start_button)
        
        # Add stop button
        self.stop_button = nuke.PyScript_Knob('stop', 'Stop Server')
        self.stop_button.setEnabled(False)
        self.addKnob(self.stop_button)
        
        # Store the server instance
        self.server = None
    
    def knobChanged(self, knob):
        """Handle knob changes"""
        if knob == self.start_button:
            self._start_server()
        elif knob == self.stop_button:
            self._stop_server()
    
    def _start_server(self):
        """Start the MCP server"""
        if self.server is None:
            port = int(self.port.value())
            self.server = NukeMCPServer(port=port)
            
            if self.server.start():
                self.status.setValue(f'Running on port {port}')
                self.start_button.setEnabled(False)
                self.stop_button.setEnabled(True)
                self.port.setEnabled(False)
            else:
                self.status.setValue('Failed to start server')
                self.server = None
    
    def _stop_server(self):
        """Stop the MCP server"""
        if self.server:
            self.server.stop()
            self.server = None
            
            self.status.setValue('Not connected')
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.port.setEnabled(True)

# Global instance of the panel
_panel = None

def show_panel():
    """Show the NukeMCP panel"""
    global _panel
    if _panel is None:
        _panel = NukeMCPPanel()
    
    # Show as a regular panel
    _panel.show()
