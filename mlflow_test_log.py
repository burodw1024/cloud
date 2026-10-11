import mlflow
import os
import os

os.environ["AWS_ACCESS_KEY_ID"] = "cB0wYpzS47LQFHRqxhoR"
os.environ["AWS_SECRET_ACCESS_KEY"] = "1nsq1fXfEkwV5LsxYPEMTGJyM4OZSCZ1n3WHqa0W"
os.environ["MLFLOW_S3_ENDPOINT_URL"] = "http://10.50.1.29:32000"

# Set the MLflow tracking URI (adjust if running externally)
mlflow.set_tracking_uri("http://192.168.1.144:30080")  # Replace with your MLflow NodePort

# Optional: set experiment name
mlflow.set_experiment("minio-test")

with mlflow.start_run():
    # Log parameters and metrics
    mlflow.log_param("param1", 5)
    mlflow.log_metric("accuracy", 0.95)

    # Create a sample artifact
    os.makedirs("outputs", exist_ok=True)
    with open("outputs/sample.txt", "w") as f:
        f.write("Hello from MLflow + MinIO!")

    # Log the artifact
    mlflow.log_artifacts("outputs")

print("Done. Check MLflow UI and your MinIO bucket!")
