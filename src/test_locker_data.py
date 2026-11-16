
import os
import pyodbc
from dotenv import load_dotenv

load_dotenv()

connection_string = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER=tcp:{os.getenv('DB_SERVER')},1433;"
    f"DATABASE={os.getenv('DB_NAME')};"
    f"UID={{{os.getenv('DB_USER')}}};"
    f"PWD={{{os.getenv('DB_PASSWORD')}}};"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30;"
)

query = """
WITH LockerCapacity AS (
    SELECT
        LockerID,
        SUM(TotalCompartments) AS TotalCapacity
    FROM dbo.LockerCompartment
    GROUP BY LockerID
),
ParcelOccupancy AS (
    SELECT
        CurrentLockerID AS LockerID,
        COUNT(*) AS Occupied
    FROM dbo.Parcel
    WHERE Status = 'AtLocker'
    GROUP BY CurrentLockerID
)
SELECT
    l.LockerID,
    l.LockerCode,
    l.LockerName,
    r.RegionName,
    l.Latitude,
    l.Longitude,
    COALESCE(p.Occupied, 0) AS Occupied,
    COALESCE(c.TotalCapacity, 0) AS TotalCapacity,
    CAST(
        100.0 * COALESCE(p.Occupied, 0)
        / NULLIF(c.TotalCapacity, 0)
        AS DECIMAL(5,1)
    ) AS UtilisationPercentage
FROM dbo.Locker l
JOIN dbo.Region r
    ON l.RegionID = r.RegionID
LEFT JOIN LockerCapacity c
    ON l.LockerID = c.LockerID
LEFT JOIN ParcelOccupancy p
    ON l.LockerID = p.LockerID
ORDER BY l.LockerID;
"""

try:
    with pyodbc.connect(connection_string) as connection:
        cursor = connection.cursor()
        cursor.execute(query)

        rows = cursor.fetchall()

        print(f"\nSuccessfully retrieved {len(rows)} lockers!\n")
        print(
            f"{'Locker':<25} {'Occupied':>10} "
            f"{'Capacity':>10} {'Utilisation':>12}"
        )
        print("-" * 62)

        for row in rows:
            print(
                f"{row.LockerName:<25} "
                f"{row.Occupied:>10} "
                f"{row.TotalCapacity:>10} "
                f"{str(row.UtilisationPercentage) + '%':>12}"
            )

except Exception as error:
    print("ERROR:")
    print(error)
