# Guidelines for Creating Nuke Nodes via MCP

When creating Nuke node graphs or compositing setups via the Nuke MCP interface, please follow these guidelines to prevent crashes:

## Critical Rules

1. **Never use numbered prefixes in node names**
   - ❌ `1_Primary_Grade`, `2_Saturation`
   - ✅ `Primary_Grade`, `Saturation`

2. **Use labels instead of renaming nodes when possible**
   - Instead of renaming nodes, set their `label` parameter
   - Example: `node["label"].setValue("Exposure Adjustment")`

3. **Create nodes incrementally**
   - Create and test nodes one at a time
   - Add connections only after confirming nodes exist
   - Use `try`/`except` blocks around each node creation

4. **Simplify node creation**
   - Start with minimal node trees (3-4 nodes maximum)
   - Use simpler node types first (Grade, Blur, Transform)
   - Avoid complex nodes like ColorSpace with string parameters

5. **Handle errors gracefully**
   - Catch exceptions for each operation
   - Print detailed error information
   - Avoid allowing one error to crash the entire setup

## Node Creation Pattern

```python
# Safe pattern
try:
    # Create node
    node = nuke.createNode("Grade", inpanel=False)
    
    # Set position
    node.setXYpos(100, 100)
    
    # Set label (instead of renaming)
    node["label"].setValue("Descriptive Label")
    
    # Only then modify parameters
    node["blackpoint"].setValue(0.1)
    
    # Only connect to existing nodes
    if 'previous_node' in locals() and previous_node:
        node.setInput(0, previous_node)
        
    # Store for next connection
    previous_node = node
    
except Exception as e:
    print(f"Error: {str(e)}")
```

## Testing Strategy

1. Start with a single node creation
2. Verify it works before proceeding
3. Add nodes incrementally
4. Test connections separately from creation

Following these guidelines will help ensure reliable operation of the Nuke MCP interface.
