import argparse
import time
import torch
from train_state_agent import predict, MLP, NUM_FEATURES, ACTIONS

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=120, help="Seconds since last message")
parser.add_argument("--hour", type=float, default=14, help="Current hour (0-23)")
parser.add_argument("--fatigue", type=float, default=0.2, help="Fatigue level (0-1)")
parser.add_argument("--social", type=float, default=0.9, help="Need for communication (0-1)")
parser.add_argument("--boredom", type=float, default=0.7, help="Boredom level (0-1)")
parser.add_argument("--dialog", type=int, default=0, choices=[0, 1], help="In active dialog (0/1)")
args = parser.parse_args()

ckpt = torch.load("state_agent.pt", map_location="cpu", weights_only=False)
model = MLP(NUM_FEATURES, ckpt["config"]["hidden_dims"], len(ACTIONS), ckpt["config"]["dropout"])
model.load_state_dict(ckpt["state_dict"])

features = [args.seconds, args.hour, args.fatigue, args.social, args.boredom, args.dialog]

start = time.perf_counter()
action, probs = predict(model, ckpt["mean"], ckpt["std"], features)
elapsed = time.perf_counter() - start

probs_list = {n: round(float(p), 3) for n, p in zip(ACTIONS, probs)}
print(f"Action: {action}")
print(f"Probs:  {probs_list}")
print(f"Inference time: {elapsed*1000:.1f} ms")
