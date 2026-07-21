import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

ACTIONS = ["wait", "sleep", "write_to_user", "plan_day"]
NUM_FEATURES = 6
NUM_CLASSES = len(ACTIONS)


@dataclass
class Config:
    data_path: str = "dataset.json"
    batch_size: int = 32
    lr: float = 1e-3
    epochs: int = 200
    hidden_dims: list = None
    dropout: float = 0.2
    weight_decay: float = 1e-4
    patience: int = 15
    test_size: float = 0.15
    val_size: float = 0.15
    seed: int = 42
    device: str = "auto"
    save_path: str = "state_agent.pt"

    def __post_init__(self):
        if self.hidden_dims is None:
            self.hidden_dims = [128, 64]
        if self.device == "auto":
            self.device = "cuda" if torch.cuda.is_available() else "cpu"


class MLP(nn.Module):
    def __init__(self, input_dim: int, hidden_dims: list[int], output_dim: int, dropout: float):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers.extend([
                nn.Linear(prev, h),
                nn.BatchNorm1d(h),
                nn.ReLU(),
                nn.Dropout(dropout),
            ])
            prev = h
        layers.append(nn.Linear(prev, output_dim))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class StateDataset(Dataset):
    def __init__(self, features: np.ndarray, labels: np.ndarray):
        self.features = torch.tensor(features, dtype=torch.float32)
        self.labels = torch.tensor(labels, dtype=torch.long)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return self.features[idx], self.labels[idx]


def load_data(path: str):
    with open(path) as f:
        raw = json.load(f)

    features, labels = [], []
    for entry in raw:
        features.append([
            entry["seconds_since_last_msg"],
            entry["current_hour"],
            entry["fatigue"],
            entry["need_social"],
            entry["boredom"],
            int(entry["in_active_dialog"]),
        ])
        labels.append(ACTIONS.index(entry["action"]))

    return np.array(features, dtype=np.float32), np.array(labels, dtype=np.int64)


def train(cfg: Config):
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    X, y = load_data(cfg.data_path)
    print(f"Loaded {len(X)} samples, {NUM_FEATURES} features, {NUM_CLASSES} classes")

    # stratified split
    X_tmp, X_test, y_tmp, y_test = train_test_split(
        X, y, test_size=cfg.test_size, stratify=y, random_state=cfg.seed
    )
    val_ratio = cfg.val_size / (1 - cfg.test_size)
    X_train, X_val, y_train, y_val = train_test_split(
        X_tmp, y_tmp, test_size=val_ratio, stratify=y_tmp, random_state=cfg.seed
    )
    print(f"Train: {len(X_train)}  Val: {len(X_val)}  Test: {len(X_test)}")

    # normalize (fit on train only)
    mean, std = X_train.mean(axis=0), X_train.std(axis=0) + 1e-8
    X_train = (X_train - mean) / std
    X_val = (X_val - mean) / std
    X_test = (X_test - mean) / std

    train_loader = DataLoader(StateDataset(X_train, y_train), cfg.batch_size, shuffle=True)
    val_loader = DataLoader(StateDataset(X_val, y_val), cfg.batch_size)
    test_loader = DataLoader(StateDataset(X_test, y_test), cfg.batch_size)

    model = MLP(NUM_FEATURES, cfg.hidden_dims, NUM_CLASSES, cfg.dropout).to(cfg.device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=0.5, patience=5
    )

    best_val_loss = float("inf")
    best_state = None
    patience_counter = 0

    for epoch in range(1, cfg.epochs + 1):
        model.train()
        train_loss = 0.0
        for Xb, yb in train_loader:
            Xb, yb = Xb.to(cfg.device), yb.to(cfg.device)
            optimizer.zero_grad()
            loss = criterion(model(Xb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item() * len(Xb)

        model.eval()
        val_loss = 0.0
        val_preds, val_true = [], []
        with torch.no_grad():
            for Xb, yb in val_loader:
                Xb, yb = Xb.to(cfg.device), yb.to(cfg.device)
                logits = model(Xb)
                loss = criterion(logits, yb)
                val_loss += loss.item() * len(Xb)
                val_preds.extend(logits.argmax(1).cpu().numpy())
                val_true.extend(yb.cpu().numpy())

        train_loss /= len(X_train)
        val_loss /= len(X_val)
        val_acc = accuracy_score(val_true, val_preds)
        scheduler.step(val_loss)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = model.state_dict()
            patience_counter = 0
        else:
            patience_counter += 1

        if epoch == 1 or epoch % 10 == 0 or patience_counter == 0:
            print(
                f"Epoch {epoch:3d}  train_loss={train_loss:.4f}  "
                f"val_loss={val_loss:.4f}  val_acc={val_acc:.4f}  "
                f"lr={optimizer.param_groups[0]['lr']:.2e}"
            )

        if patience_counter >= cfg.patience:
            print(f"Early stopping at epoch {epoch}")
            break

    # restore best and evaluate on test
    model.load_state_dict(best_state)
    model.eval()
    test_preds, test_true = [], []
    with torch.no_grad():
        for Xb, yb in test_loader:
            Xb, yb = Xb.to(cfg.device), yb.to(cfg.device)
            logits = model(Xb)
            test_preds.extend(logits.argmax(1).cpu().numpy())
            test_true.extend(yb.cpu().numpy())

    print(f"\nTest accuracy: {accuracy_score(test_true, test_preds):.4f}")
    print("\nClassification report:")
    print(classification_report(test_true, test_preds, target_names=ACTIONS, digits=3))
    print("Confusion matrix:")
    print(confusion_matrix(test_true, test_preds))

    # save
    torch.save(
        {
            "state_dict": best_state,
            "mean": mean,
            "std": std,
            "config": {"input_dim": NUM_FEATURES, "hidden_dims": cfg.hidden_dims, "output_dim": NUM_CLASSES, "dropout": cfg.dropout},
        },
        cfg.save_path,
    )
    print(f"\nModel saved to {cfg.save_path}")

    return model, (mean, std)


def predict(model, mean, std, features: list, device: str = "cpu") -> tuple[str, torch.Tensor]:
    model.eval()
    x = torch.tensor([features], dtype=torch.float32)
    x = (x - mean) / std
    with torch.no_grad():
        logits = model(x.to(device))
        probs = torch.softmax(logits, 1)
        action_idx = logits.argmax(1).item()
    return ACTIONS[action_idx], probs[0]


if __name__ == "__main__":
    cfg = Config()
    train(cfg)
