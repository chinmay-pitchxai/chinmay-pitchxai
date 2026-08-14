import asyncio
import json
import sys
import os

# Add backend dir to path
sys.path.append(os.getcwd())

from services.call_analyzer import analyze_call_transcript
from config import settings

async def test():
    transcript = """{"role": "assistant", "message": "Hello Ravi, this is Vernika."}
{"role": "user", "message": "Hi! I am interested in your AI solutions."}
{"role": "assistant", "message": "Great! Can we schedule a meeting?"}
{"role": "user", "message": "Yes, let's do it tomorrow at 10 AM."}"""
    
    print("Testing analysis with JSONL transcript...")
    result = await analyze_call_transcript(transcript)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    asyncio.run(test())
