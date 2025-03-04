from setuptools import setup, find_packages

setup(
    name="routemonitor",
    version="0.1.0",
    description="Real-Time BGP Telemetry & ML Anomaly Detection Platform",
    author="Thandava Sai Rohith Achanta",
    packages=find_packages(exclude=["tests*", "dashboard*"]),
    python_requires=">=3.9",
    install_requires=[
        "fastapi>=0.104.1",
        "uvicorn[standard]>=0.24.0",
        "pydantic>=2.5.0",
        "sqlalchemy>=2.0.23",
        "alembic>=1.12.1",
        "psycopg2-binary>=2.9.9",
        "celery>=5.3.6",
        "redis>=5.0.1",
        "influxdb-client>=1.38.0",
        "scikit-learn>=1.3.2",
        "numpy>=1.26.2",
        "scipy>=1.11.4",
        "pandas>=2.1.3",
        "httpx>=0.25.2",
        "python-dotenv>=1.0.0",
        "structlog>=23.2.0",
        "prometheus-client>=0.19.0",
    ],
)
