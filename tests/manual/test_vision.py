"""
Direct terminal test for open-interpreter vision capabilities.
Sends a small test image to verify the LMC message format works.

Run: python tests/manual/test_vision.py
"""
import sys
import os
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.tools.engine import setup_interpreter

print("Setting up interpreter with vision support...")
ai = setup_interpreter()
print(f"  supports_vision = {ai.llm.supports_vision}")
print(f"  model = {ai.llm.model}")

# Create a tiny 1x1 red PNG as test image (smallest valid PNG)
# This avoids needing an actual screenshot
import struct
import zlib

def make_tiny_png():
    """Generate a minimal 1x1 red pixel PNG in memory."""
    width, height = 1, 1
    raw_data = b'\x00\xff\x00\x00'  # filter byte + RGB
    
    def chunk(chunk_type, data):
        c = chunk_type + data
        crc = struct.pack('>I', zlib.crc32(c) & 0xffffffff)
        return struct.pack('>I', len(data)) + c + crc
    
    signature = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw_data)
    
    return signature + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

png_bytes = make_tiny_png()
b64_image = base64.b64encode(png_bytes).decode('utf-8')

print(f"\nTest image: {len(png_bytes)} bytes, base64 length: {len(b64_image)}")

# Inject the image using OI 0.4.x LMC format
ai.messages.append({
    "role": "user",
    "type": "image",
    "format": "base64.png",
    "content": b64_image
})

print("\nSending vision prompt: 'What do you see in this image? Describe it briefly.'")
print("-" * 50)

try:
    for chunk in ai.chat("What do you see in this image? Describe it briefly.", stream=True, display=False):
        if isinstance(chunk, dict):
            if chunk.get("role") in ["assistant", "computer"] and "content" in chunk:
                content = chunk["content"]
                if isinstance(content, str):
                    print(content, end="", flush=True)
    print("\n" + "-" * 50)
    print("✅ Vision test completed successfully.")
except Exception as e:
    print(f"\n❌ Vision test failed: {type(e).__name__}: {e}")
