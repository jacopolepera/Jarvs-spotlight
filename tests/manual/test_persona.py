import sys
from core.tools.engine import setup_interpreter

print("Initializing Jarvs Engine...")
ai = setup_interpreter()

prompt = "I need you to count to 3. Do not say any other words."
print(f"\nUser Prompt: {prompt}")
print("-" * 40)

# Run the inference
ai.chat(prompt, display=True)

print("\n" + "-" * 40)
print("Persona Test Complete.")
