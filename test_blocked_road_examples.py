"""
Test the three blocked-road example images against the current prompt.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 test_blocked_road_examples.py
    python3 test_blocked_road_examples.py --model openai/gpt-4o-mini
"""

import base64
import os
import sys
import time
import urllib.request

try:
    import openai
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "openai", "-q"])
    import openai

# Import prompt from the canonical source
sys.path.insert(0, os.path.dirname(__file__))
from test_single_image import HEADLINE_PROMPT, LITELLM_PROXY_URL

URLS = [
    ("Example 1", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/4/H28qO0VDPQXDfOf9K3sHxJTh8YA0.jpg?Expires=1814779565&Signature=x4p2f-Fq~f-Jvm~dUVPfFnvingpRZ-NUtUApruBd3d~z3egEVqOidjYmFeSjNhsSBy2534pGKNO0OZO6c0AiSg-998bTDSmkqRapNxJCe28TrfTXtW9yPlczaO4seC9TaNMChYRKjvxpf4JYLX6uufN4fyhCcxmN68~CSJxw~KKFr7jvw9E1GsVyTlLeL4q20vOTXuFWWl-ymRGjlOyQkAY3B1fOJTc-7DY062fywmH7P4k3TMR8HpznvpIUMIh1TPp7ibL-YIY27a22HVJGVtPQaBK-kQsRL0~wq6DxL7piaG5tLCUJZb4qXZLdcA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("Example 2", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/4/JqXcQsGl0UVjTYVMu1JdiCub0y00.jpg?Expires=1814802273&Signature=Y9XJpTSVBizcRMIuEV-6hP0NSE98NfZVjoeF~EPlhJ6M6-5cCfO~jYld5gQptS5LIRNvOiWoYFKjecrPzLXPNw-wXmOHNAMEF5Dqv~K78mGdaLTkHTI9BRjV23QZWufgWmA08FYGXKZZxoAHk-xWPJnqdLZ0~DLQqAlhvmVxa0ihjcQvEO077M4x7ETlLt6NzENaqD8VPtMAqqzglBp-90gGYyZbdt4~4H5xU2OmZQLwuIh3mHnyBklKRU4XfrQE4hOJz-a9rWLOSYu1Jsb9Cu6YEZoD31wRBXbxqqGa~xS~qTEnK5ENp824MW2NkeGuGiShHnyxKbssA-OHoQq2vw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("Example 3", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/4/RkdmosaXfC9M6bzLNYrAEg1fpzE0.jpg?Expires=1814780852&Signature=EvzROOfK0Siive6UmqW9jH3-NGOC~rILfz6qQq~-zbT1DUxAagRKjL~M-0jOPIgoz8yTJKd5-SGeEL48YLW3oXTFpqlKCGcyonZoN~sQJ3k86NxOGIsZo0UfW7T7V-omij79rqvhN1vI2H6-cD54A2jhypeki3LUN3U2Qc2gl9pDP4-s2KnExO-oGZ3iIhQrZcI2ZjIbJKws~ORkkUiCalG5NbrpAidmVhdgwcpajyqkRAIaiEsfiedSjIYlh835vqmCu4Smvh5fSkwI7M~V0wu7CXzLbJu2ids4Y5QL~F9FVvqflt0l4YEdsgin1pfrPLNYAj-aN1ebFkribC3kQw__&Key-Pair-Id=K1LXZK7POOSHBA"),
]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="openai/gpt-4o-mini")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    for label, url in URLS:
        print(f"\n{'─'*50}")
        print(f"{label}")
        print(f"{'─'*50}")
        try:
            print("  Fetching...", end=" ", flush=True)
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
                mime = resp.headers.get_content_type() or "image/jpeg"
            b64 = base64.b64encode(data).decode("utf-8")
            img = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "auto"}}
            print("ok")

            t0 = time.monotonic()
            resp = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                max_tokens=80,
                timeout=90,
            )
            latency = round((time.monotonic() - t0) * 1000)
            output = (resp.choices[0].message.content or "").strip()
            print(f"  [{args.model}] ({latency}ms)")
            print(f"  → {output.replace(chr(10), ' | ')}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\n{'─'*50}")


if __name__ == "__main__":
    main()
