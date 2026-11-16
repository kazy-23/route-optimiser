
import os
import pyodbc
from dotenv import load_dotenv

# Load credentials from the project .env file
load_dotenv()

server = os.getenv("DB_SERVER")
database = os.getenv("DB_NAME")
username = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")

if not all([server, database, username, password]):
    raise ValueError("Missing database details in .env")

# Safely format credentials for the ODBC connection string
def quote_odbc(value):
    return "{" + value.replace("}", "}}") + "}"

connection_string = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{server},1433;"
    f"DATABASE={database};"
    f"UID={quote_odbc(username)};"
    f"PWD={quote_odbc(password)};"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30;"
)

try:
    connection = pyodbc.connect(connection_string)
    cursor = connection.cursor()

    cursor.execute("SELECT DB_NAME()")
    result = cursor.fetchone()

    print("SUCCESS! Connected to Azure SQL.")
    print("Database:", result[0])

    connection.close()

except Exception as error:
    print("CONNECTION FAILED:")
    print(error)
