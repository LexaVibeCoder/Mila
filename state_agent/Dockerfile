FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY train_state_agent.py infer.py dataset_template.json .
RUN chmod 644 train_state_agent.py infer.py dataset_template.json

CMD ["python", "train_state_agent.py"]
