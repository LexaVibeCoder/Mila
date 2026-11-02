"""Interactive test for the trained State Agent MLP.

Loads ``state_agent.pt`` and asks for the six input features. Press Enter on a
prompt to accept the default, enter ``q`` to quit. After each prediction the
prompt repeats, so several inputs can be checked without restarting.

Usage:
    python test_interactive.py [--model state_agent.pt]
    python test_interactive.py --check [--model state_agent.pt]
"""

import argparse
import os
import sys
import time

import torch

from train_state_agent import ACTIONS, MLP, predict

QUIT_WORDS = {"q", "quit", "exit"}


def parse_number(raw):
    return float(raw.replace(",", "."))


def parse_hour(raw):
    value = parse_number(raw)
    if not 0 <= value <= 23:
        raise ValueError("Hour must be between 0 and 23")
    return value


def parse_fraction(raw):
    value = parse_number(raw)
    if not 0 <= value <= 1:
        raise ValueError("Value must be between 0 and 1")
    return value


def parse_dialog(raw):
    text = raw.strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1.0
    if text in {"0", "false", "no", "n"}:
        return 0.0
    raise ValueError("Please enter 0/1 (or yes/no)")


# (short name, label, default value, parser)
FIELDS = [
    ("seconds", "Seconds since last message", 120.0, parse_number),
    ("hour", "Current hour (0–23)", 14.0, parse_hour),
    ("fatigue", "Fatigue (0–1)", 0.2, parse_fraction),
    ("social", "Need for social interaction (0–1)", 0.9, parse_fraction),
    ("boredom", "Boredom (0–1)", 0.7, parse_fraction),
    ("dialog", "Active dialog (0/1)", 0.0, parse_dialog),
]


class QuitRequested(Exception):
    pass


def load_model(path):
    try:
        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        config = ckpt["config"]
        model = MLP(
            config["input_dim"],
            config["hidden_dims"],
            config["output_dim"],
            config["dropout"],
        )
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
    except FileNotFoundError:
        print(f"Error: Model not found: {path}", file=sys.stderr)
        print("First train it: train_and_test.sh (or train_and_test.bat).", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Error: Failed to load model {path}: {exc}", file=sys.stderr)
        print(
            "File is corrupted or not compatible with the current architecture; "
            "run the script with --retrain.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return model, ckpt["mean"], ckpt["std"]


def ask(label, default, parser):
    """Read one value; empty input keeps the default, ``q`` quits."""
    default_text = f"{default:g}"
    while True:
        try:
            raw = input(f"  {label} [{default_text}]: ").strip()
        except EOFError:
            raise QuitRequested
        if raw.lower() in QUIT_WORDS:
            raise QuitRequested
        if raw == "":
            return default
        try:
            return parser(raw)
        except ValueError as exc:
            print(f"  ! {exc}, try again")


def collect():
    return [ask(label, default, parser) for _, label, default, parser in FIELDS]


def show_result(action, probs, elapsed_ms):
    print(f"\n  Action: {action}")
    ranked = sorted(zip(ACTIONS, probs.tolist()), key=lambda item: item[1], reverse=True)
    for name, prob in ranked:
        print(f"    {name:<14} {prob:.3f}")
    print(f"  Inference time: {elapsed_ms:.2f} ms")


def main():
    parser = argparse.ArgumentParser(description="Interactive test State Agent MLP")
    parser.add_argument(
        "--model",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_agent.pt"),
        help="path to state_agent.pt",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check that the model exists and loads correctly",
    )
    args = parser.parse_args()

    model, mean, std = load_model(args.model)
    if args.check:
        return

    print("=== Interactive test State Agent ===")
    print("Enter — keep default value, q — quit.\n")

    while True:
        try:
            features = collect()
        except (QuitRequested, KeyboardInterrupt):
            print("\nExiting.")
            return

        start = time.perf_counter()
        action, probs = predict(model, mean, std, features)
        elapsed_ms = (time.perf_counter() - start) * 1000
        show_result(action, probs, elapsed_ms)
        print("\n  You can enter the following values (q — quit).\n")


if __name__ == "__main__":
    main()
