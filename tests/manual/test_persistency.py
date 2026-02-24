import os
from tools.engine import setup_interpreter

print("Setting up interpreter...")
ai = setup_interpreter()

print("\n--- Sending test command ---")
print("Command: Create a file called 'test_hello.txt' with the word 'hi' inside it.")

# Auto-run is enabled, so it should execute completely automatically
ai.chat("Create a file called 'test_hello.txt' with the word 'hi' inside it.", display=True)

workspace_file = os.path.join(os.path.dirname(__file__), "workspace", "test_hello.txt")
if os.path.exists(workspace_file):
    print(f"\n✅ SUCCESS: File was created inside the workspace at {workspace_file}")
    with open(workspace_file, "r") as f:
        print(f"Content: {f.read()}")
else:
    print(f"\n❌ FAIL: File was not found in the workspace at {workspace_file}")
