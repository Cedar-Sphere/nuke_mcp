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
        """Main server loop that runs in a separate thread"""
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
                                    # Attempt to parse the buffer as JSON
                                    command = json.loads(self.buffer.decode('utf-8'))
                                    # If successful, clear the buffer and process command
                                    self.buffer = b''
                                    response = self.execute_command(command)
                                    response_json = json.dumps(response)
                                    self.client.sendall(response_json.encode('utf-8'))
                                except json.JSONDecodeError:
                                    # Incomplete data, keep in buffer
                                    pass
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
            
            # Sleep to prevent CPU hogging
            time.sleep(0.1)

    def execute_command(self, command):
        """Execute a command received from the client"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            
            # Define handlers for different command types
            handlers = {
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
                "create_viewer": self.create_viewer
            }
            
            handler = handlers.get(cmd_type)
            if handler:
                try:
                    print(f"Executing handler for {cmd_type}")
                    result = handler(**params)
                    print(f"Handler execution complete")
                    return {"status": "success", "result": result}
                except Exception as e:
                    print(f"Error in handler: {str(e)}")
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
                except Exception as e:
                    print(f"Error in handler: {str(e)}")
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        except Exception as e:
            print(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}
    
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
    
    def create_node(self, node_type, name=None, position=None, inputs=None, parameters=None):
        """Create a new node in Nuke"""
        try:
            # Default parameters
            if position is None:
                position = [0, 0]
            if parameters is None:
                parameters = {}
            
            # Create the node
            node = nuke.createNode(node_type, inpanel=False)
            
            # Set name if provided
            if name:
                existing = nuke.toNode(name)
                if existing:
                    suffix = 1
                    while nuke.toNode(f"{name}_{suffix}"):
                        suffix += 1
                    name = f"{name}_{suffix}"
                node.setName(name)
            
            # Set position
            node.setXYpos(position[0], position[1])
            
            # Set parameters
            for param_name, param_value in parameters.items():
                try:
                    node[param_name].setValue(param_value)
                except Exception as e:
                    print(f"Error setting parameter {param_name}: {str(e)}")
            
            # Connect inputs if specified
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    if input_name:
                        input_node = nuke.toNode(input_name)
                        if input_node:
                            node.setInput(input_idx, input_node)
            
            # Return node information
            return {
                "name": node.name(),
                "type": node.Class(),
                "position": [node.xpos(), node.ypos()],
            }
        except Exception as e:
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
            
            # Set parameters if provided
            if parameters:
                for param_name, param_value in parameters.items():
                    try:
                        node[param_name].setValue(param_value)
                    except Exception as e:
                        print(f"Error setting parameter {param_name}: {str(e)}")
            
            # Connect inputs if specified
            if inputs:
                for input_idx, input_name in enumerate(inputs):
                    if input_name:
                        input_node = nuke.toNode(input_name)
                        if input_node:
                            node.setInput(input_idx, input_node)
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
        """Execute Python code in Nuke"""
        try:
            print(f"Executing Python code: {len(code)} characters")
            
            # Import any modules needed for the namespace
            import os
            import sys
            import math
            import random
            import time
            
            # Create a namespace with access to nuke and standard modules
            namespace = {
                'nuke': nuke,
                'nukescripts': nukescripts,
                'os': os,
                'sys': sys,
                'math': math,
                'random': random,
                'time': time,
                'output': {}  # Container for output data
            }
            
            # Add print output capturing
            old_stdout = sys.stdout
            string_io = io.StringIO()
            sys.stdout = string_io
            
            try:
                # Execute the code
                exec(code, namespace)
                namespace['output']['stdout'] = string_io.getvalue()
                
                # Return successful execution
                return {"executed": True, "output": namespace.get('output', {})}
            except Exception as e:
                # Log and return the error
                print(f"Code execution error: {str(e)}")
                traceback.print_exc()
                return {"executed": False, "error": str(e)}
            finally:
                # Always restore stdout
                sys.stdout = old_stdout
        except Exception as e:
            # Catch any other errors during setup/teardown
            print(f"Error in execute_code: {str(e)}")
            traceback.print_exc()
            return {"executed": False, "error": f"Error in execute_code: {str(e)}"}
    
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

# Note: The menu item is handled in menu.py
# nuke.menu('Nuke').addCommand('NukeMCP/Show Panel', show_panel)
