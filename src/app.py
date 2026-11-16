import html
import math
import os
import uuid
from pathlib import Path

import folium
import pyodbc
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from streamlit_folium import st_folium
from folium.plugins import PolyLineTextPath


# ============================================================
# 1. PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Parcel Network Optimiser",
    page_icon="📦",
    layout="wide",
)

PROJECT_FOLDER = Path(__file__).resolve().parent


# ============================================================
# 2. DATABASE CONNECTION
# ============================================================

def quote_odbc(value: str) -> str:
    """Safely format ODBC values containing special characters."""
    return "{" + value.replace("}", "}}") + "}"


def get_connection_string() -> str:
    load_dotenv(PROJECT_FOLDER / ".env")

    server = os.getenv("DB_SERVER")
    database = os.getenv("DB_NAME")
    username = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")

    if not all([server, database, username, password]):
        raise ValueError(
            "Database credentials are missing. "
            "Check that your .env file is in the project root."
        )

    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER=tcp:{server},1433;"
        f"DATABASE={database};"
        f"UID={quote_odbc(username)};"
        f"PWD={quote_odbc(password)};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


# ============================================================
# 3. RETRIEVE LOCKER AND OCCUPANCY DATA
# ============================================================

SQL_QUERY = """
WITH LockerCapacity AS (
    SELECT
        LockerID,
        SUM(TotalCompartments) AS TotalCapacity,
        SUM(CASE WHEN CompartmentSize = 'Small'
                 THEN TotalCompartments ELSE 0 END) AS SmallCapacity,
        SUM(CASE WHEN CompartmentSize = 'Medium'
                 THEN TotalCompartments ELSE 0 END) AS MediumCapacity,
        SUM(CASE WHEN CompartmentSize = 'Large'
                 THEN TotalCompartments ELSE 0 END) AS LargeCapacity
    FROM dbo.LockerCompartment
    GROUP BY LockerID
),
ParcelOccupancy AS (
    SELECT
        CurrentLockerID AS LockerID,
        COUNT(*) AS Occupied,
        SUM(CASE WHEN ParcelSize = 'Small' THEN 1 ELSE 0 END)
            AS SmallOccupied,
        SUM(CASE WHEN ParcelSize = 'Medium' THEN 1 ELSE 0 END)
            AS MediumOccupied,
        SUM(CASE WHEN ParcelSize = 'Large' THEN 1 ELSE 0 END)
            AS LargeOccupied
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
    COALESCE(p.SmallOccupied, 0) AS SmallOccupied,
    COALESCE(c.SmallCapacity, 0) AS SmallCapacity,
    COALESCE(p.MediumOccupied, 0) AS MediumOccupied,
    COALESCE(c.MediumCapacity, 0) AS MediumCapacity,
    COALESCE(p.LargeOccupied, 0) AS LargeOccupied,
    COALESCE(c.LargeCapacity, 0) AS LargeCapacity,
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
WHERE l.IsActive = 1
ORDER BY l.LockerID;
"""


@st.cache_data(ttl=60, show_spinner="Connecting to Azure SQL and retrieving lockers...")
def load_lockers():
    connection_string = get_connection_string()
    lockers = []

    with pyodbc.connect(connection_string) as connection:
        cursor = connection.cursor()
        cursor.execute(SQL_QUERY)
        rows = cursor.fetchall()

        for row in rows:
            capacity = int(row.TotalCapacity or 0)
            occupied = int(row.Occupied or 0)

            lockers.append({
                "id": int(row.LockerID),
                "code": str(row.LockerCode),
                "name": str(row.LockerName),
                "region": str(row.RegionName),
                "lat": float(row.Latitude),
                "lon": float(row.Longitude),
                "occupied": occupied,
                "capacity": capacity,
                "small_occupied": int(row.SmallOccupied or 0),
                "small_capacity": int(row.SmallCapacity or 0),
                "medium_occupied": int(row.MediumOccupied or 0),
                "medium_capacity": int(row.MediumCapacity or 0),
                "large_occupied": int(row.LargeOccupied or 0),
                "large_capacity": int(row.LargeCapacity or 0),
                "utilisation": (
                    float(row.UtilisationPercentage)
                    if row.UtilisationPercentage is not None
                    else 0.0
                ),
            })

    if not lockers:
        raise ValueError("No active lockers were returned from the database.")

    return lockers


# ============================================================
# 3B. PARCEL MANAGEMENT DATA AND SAFE TRANSFER OPERATIONS
# ============================================================

@st.cache_data(ttl=30, show_spinner="Retrieving parcels currently stored in lockers...")
def load_parcels():
    query = """
    SELECT
        p.ParcelID, p.ParcelCode, p.ParcelSize, p.CreatedAt,
        p.ExpectedDeliveryAt, p.LastStatusAt,
        p.CurrentLockerID, l.LockerCode, l.LockerName
    FROM dbo.Parcel p
    JOIN dbo.Locker l ON l.LockerID = p.CurrentLockerID
    WHERE p.Status = 'AtLocker'
      AND l.IsActive = 1
    ORDER BY p.ParcelID;
    """
    with pyodbc.connect(get_connection_string()) as connection:
        rows = connection.cursor().execute(query).fetchall()
    return [
        {
            "id": int(r.ParcelID),
            "code": str(r.ParcelCode),
            "size": str(r.ParcelSize),
            "created": r.CreatedAt,
            "expected": r.ExpectedDeliveryAt,
            "last_status": r.LastStatusAt,
            "locker_id": int(r.CurrentLockerID),
            "locker_code": str(r.LockerCode),
            "locker_name": str(r.LockerName),
        }
        for r in rows
    ]


@st.cache_data(ttl=60, show_spinner=False)
def load_transfer_recommendations():
    """Load every feasible parcel/locker pair; no TOP-N truncation."""
    query = """
    SELECT
        c.ParcelID, c.ParcelCode, c.ParcelSize,
        c.SourceLockerID, c.SourceLockerCode, c.SourceLockerName,
        sr.RegionName AS SourceRegionName,
        c.DestinationLockerID, c.DestinationLockerCode, c.DestinationLockerName,
        dr.RegionName AS DestinationRegionName,
        c.AvailableMatchingCompartments,
        c.SourceSizeUtilisationPercent, c.DestinationSizeUtilisationPercent,
        c.SourceOverallUtilisationPercent, c.DestinationOverallUtilisationPercent,
        c.StraightLineDistanceKm, c.EstimatedRoadDistanceKm,
        c.EstimatedTransportCostEUR,
        CASE WHEN EXISTS (
            SELECT 1
            FROM dbo.ParcelMovement pm
            WHERE pm.ParcelID = c.ParcelID
              AND pm.EventType = 'Transferred'
              AND pm.EventAt >= DATEADD(HOUR, -24, SYSDATETIME())
        ) THEN 1 ELSE 0 END AS RecentTransferWithin24Hours
    FROM dbo.v_TransferCandidates c
    JOIN dbo.Locker sl ON sl.LockerID = c.SourceLockerID
    JOIN dbo.Region sr ON sr.RegionID = sl.RegionID
    JOIN dbo.Locker dl ON dl.LockerID = c.DestinationLockerID
    JOIN dbo.Region dr ON dr.RegionID = dl.RegionID
    WHERE c.AvailableMatchingCompartments > 0
    ORDER BY c.ParcelID, c.EstimatedTransportCostEUR, c.DestinationLockerID;
    """
    with pyodbc.connect(get_connection_string()) as connection:
        cursor = connection.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]



# ============================================================
# 3C. COMMITTED TRANSPORT JOBS AND VEHICLE ROUTING
# ============================================================

TRANSPORT_SCHEMA_SQL = """
IF OBJECT_ID('dbo.TransportPlan', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.TransportPlan (
        TransportPlanID UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
        PlanName NVARCHAR(200) NOT NULL,
        PlanStatus NVARCHAR(30) NOT NULL,
        CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_TransportPlan_CreatedAt DEFAULT SYSDATETIME(),
        SourceSnapshotAt DATETIME2 NULL,
        TotalParcels INT NOT NULL,
        Notes NVARCHAR(1000) NULL
    );
END;
IF OBJECT_ID('dbo.TransportPlanLine', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.TransportPlanLine (
        TransportPlanLineID BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        TransportPlanID UNIQUEIDENTIFIER NOT NULL,
        ParcelID INT NOT NULL,
        ParcelCode NVARCHAR(100) NOT NULL,
        ParcelSize NVARCHAR(20) NOT NULL,
        SourceLockerID INT NOT NULL,
        DestinationLockerID INT NOT NULL,
        EstimatedRoadDistanceKm DECIMAL(10,2) NULL,
        EstimatedTransferCostEUR DECIMAL(12,2) NULL,
        CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_TransportPlanLine_CreatedAt DEFAULT SYSDATETIME(),
        CONSTRAINT FK_TransportPlanLine_Plan FOREIGN KEY (TransportPlanID)
            REFERENCES dbo.TransportPlan(TransportPlanID),
        CONSTRAINT UQ_TransportPlanLine_PlanParcel UNIQUE (TransportPlanID, ParcelID)
    );
END;
"""


def ensure_transport_schema(cursor):
    cursor.execute(TRANSPORT_SCHEMA_SQL)


def commit_transport_plan(recommendations, generated_at=None):
    """Persist approved proposed movements as transport jobs; never moves parcels."""
    if not recommendations:
        raise ValueError("There are no recommended parcel movements to commit.")
    plan_id = str(uuid.uuid4())
    connection = pyodbc.connect(get_connection_string(), autocommit=False)
    try:
        cursor = connection.cursor()
        ensure_transport_schema(cursor)
        parcel_ids = [int(x["ParcelID"]) for x in recommendations]
        placeholders = ",".join("?" for _ in parcel_ids)
        cursor.execute(
            f"""SELECT COUNT(*) FROM dbo.TransportPlanLine l
                JOIN dbo.TransportPlan h ON h.TransportPlanID=l.TransportPlanID
                WHERE h.PlanStatus IN ('Committed','Routing')
                  AND l.ParcelID IN ({placeholders})""",
            parcel_ids,
        )
        already_committed = int(cursor.fetchone()[0] or 0)
        if already_committed:
            raise ValueError(
                f"{already_committed} parcel(s) are already in an active committed transport plan. "
                "Refresh the optimiser and review existing jobs before committing again."
            )
        cursor.execute(
            """INSERT INTO dbo.TransportPlan
               (TransportPlanID, PlanName, PlanStatus, SourceSnapshotAt, TotalParcels, Notes)
               VALUES (?, ?, 'Committed', ?, ?, ?)""",
            plan_id,
            f"Network transfer plan {plan_id[:8]}",
            generated_at,
            len(recommendations),
            "Committed for transport planning only; parcel records have not been moved.",
        )
        for item in recommendations:
            cursor.execute(
                """INSERT INTO dbo.TransportPlanLine
                   (TransportPlanID, ParcelID, ParcelCode, ParcelSize,
                    SourceLockerID, DestinationLockerID,
                    EstimatedRoadDistanceKm, EstimatedTransferCostEUR)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                plan_id, int(item["ParcelID"]), str(item["ParcelCode"]),
                str(item["ParcelSize"]), int(item["SourceLockerID"]),
                int(item["DestinationLockerID"]),
                float(item.get("EstimatedRoadDistanceKm") or 0),
                float(item.get("EstimatedTransportCostEUR") or 0),
            )
        connection.commit()
        return plan_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_active_transport_plans():
    """Return committed plans that can still be cancelled by the operator."""
    query = """
    SELECT h.TransportPlanID, h.PlanName, h.CreatedAt, h.TotalParcels,
           COUNT(l.ParcelID) AS CurrentParcelCount,
           COALESCE(h.Notes, '') AS Notes
    FROM dbo.TransportPlan h
    LEFT JOIN dbo.TransportPlanLine l ON l.TransportPlanID=h.TransportPlanID
    WHERE h.PlanStatus='Committed'
    GROUP BY h.TransportPlanID, h.PlanName, h.CreatedAt, h.TotalParcels, h.Notes
    ORDER BY h.CreatedAt DESC;
    """
    with pyodbc.connect(get_connection_string()) as connection:
        cursor = connection.cursor()
        ensure_transport_schema(cursor)
        cursor.execute(query)
        columns = [x[0] for x in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def cancel_transport_plan(plan_id, reason=None):
    """Cancel a committed transport plan without changing parcel records."""
    try:
        plan_uuid = str(uuid.UUID(str(plan_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("The selected transport plan reference is invalid.")

    connection = pyodbc.connect(get_connection_string(), autocommit=False)
    try:
        cursor = connection.cursor()
        ensure_transport_schema(cursor)
        cursor.execute(
            """UPDATE dbo.TransportPlan
               SET PlanStatus='Cancelled',
                   Notes=CONCAT(COALESCE(Notes, ''),
                       CASE WHEN COALESCE(Notes, '')='' THEN '' ELSE ' | ' END,
                       'Cancelled by operator at ', CONVERT(NVARCHAR(30), SYSDATETIME(), 126),
                       CASE WHEN ? IS NULL OR LTRIM(RTRIM(?))='' THEN '' ELSE CONCAT('. Reason: ', ?) END)
               WHERE TransportPlanID=? AND PlanStatus='Committed';""",
            reason, reason, reason, plan_uuid,
        )
        if cursor.rowcount != 1:
            raise ValueError(
                "This transport plan is no longer active or cannot be cancelled. "
                "Refresh the page and check its current status."
            )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_committed_transport_jobs():
    query = """
    SELECT h.TransportPlanID, h.PlanName, h.CreatedAt AS PlanCreatedAt,
           l.ParcelID, l.ParcelCode, l.ParcelSize,
           l.SourceLockerID, sl.LockerName AS SourceLockerName,
           sr.RegionName AS SourceRegionName,
           sl.Latitude AS SourceLatitude, sl.Longitude AS SourceLongitude, sl.LockerCode AS SourceLockerCode,
           l.DestinationLockerID, dl.LockerName AS DestinationLockerName,
           dr.RegionName AS DestinationRegionName,
           dl.Latitude AS DestinationLatitude, dl.Longitude AS DestinationLongitude, dl.LockerCode AS DestinationLockerCode,
           l.EstimatedRoadDistanceKm, l.EstimatedTransferCostEUR
    FROM dbo.TransportPlan h
    JOIN dbo.TransportPlanLine l ON l.TransportPlanID=h.TransportPlanID
    JOIN dbo.Locker sl ON sl.LockerID=l.SourceLockerID
    JOIN dbo.Region sr ON sr.RegionID=sl.RegionID
    JOIN dbo.Locker dl ON dl.LockerID=l.DestinationLockerID
    JOIN dbo.Region dr ON dr.RegionID=dl.RegionID
    WHERE h.PlanStatus='Committed'
    ORDER BY h.CreatedAt, h.TransportPlanID, l.ParcelID;
    """
    with pyodbc.connect(get_connection_string()) as connection:
        cursor = connection.cursor()
        ensure_transport_schema(cursor)
        cursor.execute(query)
        columns = [x[0] for x in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]



def execute_transport_plan(plan_id):
    """Atomically execute every parcel movement in one committed plan."""
    try:
        plan_uuid = str(uuid.UUID(str(plan_id)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("The selected transport plan reference is invalid.")

    connection = pyodbc.connect(get_connection_string(), autocommit=False)
    try:
        cursor = connection.cursor()
        cursor.execute("SET XACT_ABORT ON;")
        cursor.execute("""
            SELECT PlanStatus FROM dbo.TransportPlan WITH (UPDLOCK, HOLDLOCK)
            WHERE TransportPlanID=?;
        """, plan_uuid)
        header = cursor.fetchone()
        if header is None or str(header.PlanStatus) != "Committed":
            raise ValueError("This plan is no longer committed. Refresh and check its status.")

        cursor.execute("""
            SELECT ParcelID, ParcelCode, ParcelSize, SourceLockerID, DestinationLockerID
            FROM dbo.TransportPlanLine WITH (UPDLOCK, HOLDLOCK)
            WHERE TransportPlanID=? ORDER BY ParcelID;
        """, plan_uuid)
        lines = cursor.fetchall()
        if not lines:
            raise ValueError("This transport plan contains no parcel movements.")

        # Lock and validate every parcel before changing any of them.
        parcel_rows = []
        for line in lines:
            cursor.execute("""
                SELECT ParcelID, ParcelCode, ParcelSize, CurrentLockerID
                FROM dbo.Parcel WITH (UPDLOCK, HOLDLOCK)
                WHERE ParcelID=? AND Status='AtLocker';
            """, int(line.ParcelID))
            parcel = cursor.fetchone()
            if parcel is None:
                raise ValueError(f"{line.ParcelCode} is no longer stored in a locker. Nothing was changed.")
            if int(parcel.CurrentLockerID) != int(line.SourceLockerID):
                raise ValueError(f"{line.ParcelCode} is no longer at its planned source locker. Nothing was changed.")
            if str(parcel.ParcelSize) != str(line.ParcelSize):
                raise ValueError(f"{line.ParcelCode} has a different parcel size than the plan. Nothing was changed.")
            if int(line.SourceLockerID) == int(line.DestinationLockerID):
                raise ValueError(f"{line.ParcelCode} has the same source and destination. Nothing was changed.")
            parcel_rows.append((line, parcel))

        # Validate destination compartments using net changes by locker and size.
        deltas = {}
        for line, parcel in parcel_rows:
            deltas[(int(line.SourceLockerID), str(line.ParcelSize))] = deltas.get((int(line.SourceLockerID), str(line.ParcelSize)), 0) - 1
            deltas[(int(line.DestinationLockerID), str(line.ParcelSize))] = deltas.get((int(line.DestinationLockerID), str(line.ParcelSize)), 0) + 1
        for (locker_id, size), delta in deltas.items():
            cursor.execute("""
                SELECT
                    COALESCE((SELECT SUM(TotalCompartments) FROM dbo.LockerCompartment
                              WHERE LockerID=? AND CompartmentSize=?),0) AS Capacity,
                    (SELECT COUNT(*) FROM dbo.Parcel WITH (UPDLOCK, HOLDLOCK)
                     WHERE CurrentLockerID=? AND ParcelSize=? AND Status='AtLocker') AS Occupied
            """, locker_id, size, locker_id, size)
            row = cursor.fetchone()
            capacity, occupied = int(row.Capacity or 0), int(row.Occupied or 0)
            if occupied + delta > capacity:
                raise ValueError(
                    f"Insufficient {size.lower()} capacity at locker {locker_id}: "
                    f"projected occupancy {occupied + delta}/{capacity}. Nothing was changed."
                )
            if occupied + delta < 0:
                raise ValueError(f"Invalid source occupancy at locker {locker_id}. Nothing was changed.")

        cursor.execute("""
            SELECT CASE WHEN COL_LENGTH('dbo.ParcelMovement', 'StraightLineDistanceKm') IS NOT NULL
                        AND COL_LENGTH('dbo.ParcelMovement', 'EstimatedRoadDistanceKm') IS NOT NULL
                        AND COL_LENGTH('dbo.ParcelMovement', 'EstimatedTransportCostEUR') IS NOT NULL
                        THEN 1 ELSE 0 END;
        """)
        has_cost_columns = bool(cursor.fetchone()[0])
        cursor.execute("""
            SELECT RoadDistanceFactor, CostPerKmEUR, FixedHandlingCostEUR
            FROM dbo.TransportCostSettings WHERE SettingID=1;
        """)
        settings = cursor.fetchone()
        factor = float(settings.RoadDistanceFactor) if settings else 1.30
        cost_per_km = float(settings.CostPerKmEUR) if settings else 0.45
        fixed_cost = float(settings.FixedHandlingCostEUR) if settings else 1.50

        cursor.execute("SELECT SYSDATETIME();")
        event_time = cursor.fetchone()[0]
        for line, parcel in parcel_rows:
            src_id, dst_id = int(line.SourceLockerID), int(line.DestinationLockerID)
            cursor.execute("SELECT Latitude, Longitude FROM dbo.Locker WHERE LockerID=?", src_id)
            src = cursor.fetchone()
            cursor.execute("SELECT Latitude, Longitude FROM dbo.Locker WHERE LockerID=?", dst_id)
            dst = cursor.fetchone()
            if not src or not dst or any(v is None for v in [src.Latitude, src.Longitude, dst.Latitude, dst.Longitude]):
                raise ValueError(f"Missing coordinates for movement {parcel.ParcelCode}. Nothing was changed.")
            straight = haversine_km(src.Latitude, src.Longitude, dst.Latitude, dst.Longitude)
            road = straight * factor
            cost = fixed_cost + road * cost_per_km
            cursor.execute("""
                UPDATE dbo.Parcel SET CurrentLockerID=?, LastStatusAt=?
                WHERE ParcelID=? AND Status='AtLocker' AND CurrentLockerID=?;
            """, dst_id, event_time, int(line.ParcelID), src_id)
            if cursor.rowcount != 1:
                raise ValueError(f"Could not update {parcel.ParcelCode}. The transaction was rolled back.")
            note = f"Executed from approved transport plan {plan_uuid}; distance and cost are illustrative estimates."
            if has_cost_columns:
                cursor.execute("""
                    INSERT INTO dbo.ParcelMovement
                    (ParcelID, FromLockerID, ToLockerID, EventType, EventAt, Notes,
                     StraightLineDistanceKm, EstimatedRoadDistanceKm, EstimatedTransportCostEUR)
                    VALUES (?, ?, ?, 'Transferred', ?, ?, ?, ?, ?);
                """, int(line.ParcelID), src_id, dst_id, event_time, note, straight, road, cost)
            else:
                cursor.execute("""
                    INSERT INTO dbo.ParcelMovement
                    (ParcelID, FromLockerID, ToLockerID, EventType, EventAt, Notes)
                    VALUES (?, ?, ?, 'Transferred', ?, ?);
                """, int(line.ParcelID), src_id, dst_id, event_time, note)

        cursor.execute("""
            UPDATE dbo.TransportPlan
            SET PlanStatus='Completed',
                Notes=CONCAT(COALESCE(Notes,''), ' | Executed in Azure SQL at ', CONVERT(NVARCHAR(30), SYSDATETIME(), 126))
            WHERE TransportPlanID=? AND PlanStatus='Committed';
        """, plan_uuid)
        if cursor.rowcount != 1:
            raise ValueError("The plan status changed during execution. The transaction was rolled back.")
        connection.commit()
        return len(parcel_rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def haversine_km(a_lat, a_lon, b_lat, b_lon):
    radius = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat-a_lat)
    dl = math.radians(b_lon-a_lon)
    value = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def choose_central_depot(jobs):
    """Choose the available job locker minimising total estimated access distance to all job endpoints."""
    candidates={}
    endpoints=[]
    for job in jobs:
        src=(int(job["SourceLockerID"]),float(job["SourceLatitude"]),float(job["SourceLongitude"]))
        dst=(int(job["DestinationLockerID"]),float(job["DestinationLatitude"]),float(job["DestinationLongitude"]))
        candidates[src[0]]=src; candidates[dst[0]]=dst
        endpoints.extend([src,dst])
    if not candidates:
        raise ValueError("No locker coordinates are available to choose a depot.")
    return min(candidates.values(),key=lambda c:sum(haversine_km(c[1],c[2],p[1],p[2]) for p in endpoints))[0]


def optimise_vehicle_routes(jobs, depot_locker_id, vehicle_count, vehicle_capacity,
                            eur_per_km, fixed_vehicle_cost, road_factor=1.30,
                            average_speed_kmh=45.0, service_minutes=8,
                            time_value_eur_per_hour=12.0):
    """Multi-vehicle pickup/delivery VRP with estimated travel and handling time."""
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:
        raise RuntimeError("OR-Tools is missing. Install it with: python -m pip install ortools") from exc
    if not jobs:
        raise ValueError("There are no committed transport jobs waiting for routing.")
    lockers = {}
    for job in jobs:
        lockers[int(job["SourceLockerID"])] = (str(job["SourceLockerName"]), float(job["SourceLatitude"]), float(job["SourceLongitude"]))
        lockers[int(job["DestinationLockerID"])] = (str(job["DestinationLockerName"]), float(job["DestinationLatitude"]), float(job["DestinationLongitude"]))
    if int(depot_locker_id) not in lockers:
        raise ValueError("Choose a depot that is included in the committed jobs.")

    lane_map = {}
    for job in jobs:
        lane_map.setdefault((int(job["SourceLockerID"]), int(job["DestinationLockerID"])), []).append(job)
    nodes = [{"locker_id": int(depot_locker_id), "kind": "Depot", "demand": 0, "lane": None, "jobs": []}]
    pairs = []
    for (src, dst), lane_jobs in lane_map.items():
        q = len(lane_jobs)
        pi = len(nodes); nodes.append({"locker_id": src, "kind": "Pickup", "demand": q, "lane": (src,dst), "jobs": lane_jobs})
        di = len(nodes); nodes.append({"locker_id": dst, "kind": "Drop-off", "demand": -q, "lane": (src,dst), "jobs": lane_jobs})
        pairs.append((pi, di))

    manager = pywrapcp.RoutingIndexManager(len(nodes), int(vehicle_count), 0)
    routing = pywrapcp.RoutingModel(manager)
    coords = [(lockers[n["locker_id"]][1], lockers[n["locker_id"]][2]) for n in nodes]
    n_nodes = len(nodes)
    km_matrix = [[haversine_km(*coords[i], *coords[j]) * float(road_factor) for j in range(n_nodes)] for i in range(n_nodes)]
    mins_matrix = [[int(round(km_matrix[i][j] / max(average_speed_kmh, 1) * 60)) for j in range(n_nodes)] for i in range(n_nodes)]
    cents_matrix = [[int(round(km_matrix[i][j] * float(eur_per_km) * 100)) for j in range(n_nodes)] for i in range(n_nodes)]

    def arc_cost(fi, ti):
        return cents_matrix[manager.IndexToNode(fi)][manager.IndexToNode(ti)]
    cost_idx = routing.RegisterTransitCallback(arc_cost)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_idx)
    for v in range(int(vehicle_count)):
        routing.SetFixedCostOfVehicle(int(round(float(fixed_vehicle_cost)*100)), v)

    def time_callback(fi, ti):
        i, j = manager.IndexToNode(fi), manager.IndexToNode(ti)
        handling = int(service_minutes) if nodes[i]["kind"] in ("Pickup", "Drop-off") else 0
        return mins_matrix[i][j] + handling
    time_idx = routing.RegisterTransitCallback(time_callback)
    routing.AddDimension(time_idx, 0, 7*24*60, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    # Penalise the time until the LAST truck finishes, rather than summing
    # truck-hours. This gives the solver a reason to split work across vehicles.
    time_dim.SetGlobalSpanCostCoefficient(max(1, int(round(float(time_value_eur_per_hour) * 100 / 60))))

    def demand_callback(index):
        return int(nodes[manager.IndexToNode(index)]["demand"])
    demand_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [int(vehicle_capacity)]*int(vehicle_count), True, "Load")
    load_dim = routing.GetDimensionOrDie("Load")
    for pi, di in pairs:
        p_idx, d_idx = manager.NodeToIndex(pi), manager.NodeToIndex(di)
        routing.AddPickupAndDelivery(p_idx, d_idx)
        routing.solver().Add(routing.VehicleVar(p_idx) == routing.VehicleVar(d_idx))
        routing.solver().Add(time_dim.CumulVar(p_idx) <= time_dim.CumulVar(d_idx))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 3
    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise ValueError("No feasible route plan found. Increase the fleet limit or vehicle capacity.")

    routes, total_km, used = [], 0.0, 0
    total_minutes = 0
    route_durations = []
    for vehicle in range(int(vehicle_count)):
        index = routing.Start(vehicle)
        stops, route_km, route_minutes = [], 0.0, 0
        onboard = {}
        previous_node = 0
        while not routing.IsEnd(index):
            ni = manager.IndexToNode(index)
            node = nodes[ni]
            if node["kind"] in ("Pickup", "Drop-off"):
                lane_jobs = node["jobs"]
                codes = [str(x["ParcelCode"]) for x in lane_jobs]
                if node["kind"] == "Pickup":
                    for job in lane_jobs: onboard[int(job["ParcelID"])] = job
                else:
                    for job in lane_jobs: onboard.pop(int(job["ParcelID"]), None)
                name, lat, lon = lockers[node["locker_id"]]
                stops.append({"Stop":len(stops)+1,"Action":node["kind"],"Locker":name,
                    "LockerCode":str(lane_jobs[0].get("SourceLockerCode") if node["kind"]=="Pickup" else lane_jobs[0].get("DestinationLockerCode") or name),
                    "LockerID":node["locker_id"],"Latitude":lat,"Longitude":lon,
                    "Parcels":len(codes),"ParcelCodes":codes,
                    "ElapsedMinutes":int(solution.Value(time_dim.CumulVar(index))),
                    "ParcelDetails":[{"Code":str(j["ParcelCode"]),"Size":str(j["ParcelSize"])} for j in lane_jobs],
                    "OnboardAfterStop":[{"Code":str(j["ParcelCode"]),"Size":str(j["ParcelSize"])} for j in onboard.values()]})
            nxt = solution.Value(routing.NextVar(index))
            nj = manager.IndexToNode(nxt)
            route_km += km_matrix[ni][nj]
            route_minutes += time_callback(index, nxt)
            index = nxt
        if stops:
            used += 1
            total_km += route_km
            total_minutes += route_minutes
            route_durations.append(int(route_minutes))
            routes.append({"Vehicle":f"Truck {vehicle+1}","Stops":stops,"DistanceKm":round(route_km,2),
                           "DurationMinutes":int(route_minutes),"DurationHours":route_minutes/60,
                           "PeakLoadParcels":max((len(x["OnboardAfterStop"]) for x in stops),default=0)})
    variable = total_km*float(eur_per_km)
    fixed = used*float(fixed_vehicle_cost)
    completion_minutes = max(route_durations, default=0)
    completion_hours = completion_minutes / 60
    return {"routes":routes,"total_km":round(total_km,2),"vehicles_used":used,
        "variable_cost":variable,"fixed_cost":fixed,"total_cost":variable+fixed,
        "job_count":len(jobs),"parcel_count":len({int(x["ParcelID"]) for x in jobs}),
        "road_factor":float(road_factor),"total_minutes":int(total_minutes),
        "total_hours":completion_hours,"completion_minutes":completion_minutes,
        "truck_hours":total_minutes/60,"average_speed_kmh":float(average_speed_kmh),
        "service_minutes":int(service_minutes),"time_value":float(time_value_eur_per_hour),
        "objective_score":variable+fixed+completion_hours*float(time_value_eur_per_hour)}


def _job_components(jobs):
    """Weakly connected transfer groups. Each group needs a locally based vehicle."""
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    for j in jobs:
        union(int(j["SourceLockerID"]), int(j["DestinationLockerID"]))
    groups = {}
    for j in jobs:
        root = find(int(j["SourceLockerID"]))
        groups.setdefault(root, []).append(j)
    return sorted(groups.values(), key=lambda g: (-len(g), min(int(x["SourceLockerID"]) for x in g)))


def choose_fleet_plan(jobs, depot_id, max_vehicles, capacity, eur_per_km, fixed_cost,
                      road_factor, speed, service_minutes, time_value, lockers=None,
                      exact_vehicles=None):
    """Optimise each disconnected transfer component with its own local starting locker.

    A component is a weakly connected group of source/destination lockers. At least
    one truck is assigned to each component, so a vehicle cannot teleport from the
    central depot to an unrelated region. Additional vehicles are distributed across
    components in proportion to their parcel workload.
    """
    if not jobs:
        raise ValueError("There are no committed transport jobs waiting for routing.")
    components = _job_components(jobs)
    minimum_fleet = len(components)
    if int(max_vehicles) < minimum_fleet:
        raise ValueError(
            f"At least {minimum_fleet} trucks are required because the committed movements "
            f"form {minimum_fleet} disconnected geographic groups. Increase the maximum available trucks."
        )
    locker_lookup = {int(x["id"]): x for x in (lockers or [])}
    source_ids = {int(j["SourceLockerID"]) for j in jobs}
    source_utils = [float(locker_lookup[i].get("utilisation", 0)) for i in source_ids if i in locker_lookup]
    congestion = (sum(source_utils) / len(source_utils) / 100) if source_utils else 0.0
    effective_time_value = float(time_value) * (1.0 + 2.0 * congestion)

    options = []
    if exact_vehicles is not None:
        exact_vehicles = int(exact_vehicles)
        if exact_vehicles < minimum_fleet:
            raise ValueError(
                f"At least {minimum_fleet} trucks are required for the {minimum_fleet} "
                "disconnected transfer groups. Increase the manual fleet size."
            )
        if exact_vehicles > int(max_vehicles):
            raise ValueError("Manual truck count cannot exceed the available fleet limit.")
        fleet_sizes_to_test = [exact_vehicles]
    else:
        fleet_sizes_to_test = range(minimum_fleet, int(max_vehicles) + 1)
    for total_trucks in fleet_sizes_to_test:
        allocations = [1] * minimum_fleet
        remaining = total_trucks - minimum_fleet
        # Assign additional vehicles to the currently most heavily loaded component.
        for _ in range(remaining):
            idx = max(range(minimum_fleet), key=lambda i: len(components[i]) / allocations[i])
            allocations[idx] += 1
        component_results = []
        for group, n_trucks in zip(components, allocations):
            # Start each component's vehicles at its busiest origin locker.
            origin_counts = {}
            for j in group:
                sid = int(j["SourceLockerID"])
                origin_counts[sid] = origin_counts.get(sid, 0) + 1
            local_start = max(origin_counts, key=origin_counts.get)
            component_results.append(optimise_vehicle_routes(
                group, local_start, n_trucks, capacity, eur_per_km, fixed_cost,
                road_factor, speed, service_minutes, effective_time_value
            ))
        routes = []
        for ci, cr in enumerate(component_results, 1):
            for route in cr["routes"]:
                route["Vehicle"] = f"Truck {len(routes)+1}"
                route["Component"] = ci
                routes.append(route)
        # Defensive final renumbering: the displayed fleet size and route labels must agree.
        for route_number, route in enumerate(routes, 1):
            route["Vehicle"] = f"Truck {route_number}"
        used = len(routes)
        total_km = sum(x["total_km"] for x in component_results)
        variable_cost = sum(x["variable_cost"] for x in component_results)
        fixed_vehicle_cost_total = used * float(fixed_cost)
        completion_hours = max((x["total_hours"] for x in component_results), default=0.0)
        truck_hours = sum(x["truck_hours"] for x in component_results)
        total_cost = variable_cost + fixed_vehicle_cost_total
        score = total_cost + completion_hours * effective_time_value
        options.append({
            "routes": routes, "total_km": round(total_km, 2), "vehicles_used": used,
            "variable_cost": variable_cost, "fixed_cost": fixed_vehicle_cost_total,
            "total_cost": total_cost, "job_count": len(jobs),
            "parcel_count": len({int(x["ParcelID"]) for x in jobs}),
            "total_hours": completion_hours, "truck_hours": truck_hours,
            "objective_score": score, "fleet_limit_tested": total_trucks,
            "component_count": minimum_fleet, "component_allocations": allocations,
            "source_congestion_percent": congestion * 100,
            "effective_time_value": effective_time_value,
        })
    # Keep one row per actual truck count. Several fleet limits can yield the same
    # number of used vehicles; retain only the cheapest score for that actual count.
    dedup = {}
    for option in options:
        key = option["vehicles_used"]
        if key not in dedup or option["objective_score"] < dedup[key]["objective_score"]:
            dedup[key] = option
    options = sorted(dedup.values(), key=lambda x: x["vehicles_used"])
    best = min(options, key=lambda x: x["objective_score"])
    cheapest = min(options, key=lambda x: x["total_cost"])
    fastest = min(options, key=lambda x: x["total_hours"])
    one_truck = next((x for x in options if x["vehicles_used"] == 1), None)
    baseline_hours = one_truck["total_hours"] if one_truck else max(x["total_hours"] for x in options)
    best["base_time_value"] = float(time_value)
    best["cheapest_option"] = {"vehicles": cheapest["vehicles_used"], "cost": cheapest["total_cost"], "hours": cheapest["total_hours"]}
    best["fastest_option"] = {"vehicles": fastest["vehicles_used"], "cost": fastest["total_cost"], "hours": fastest["total_hours"]}
    best["fleet_comparison"] = [{
        "Vehicles": x["vehicles_used"], "Completion time (hours)": round(x["total_hours"], 2),
        "Total truck-hours": round(x["truck_hours"], 2),
        "Completion time saved vs baseline (h)": round(max(0, baseline_hours-x["total_hours"]), 2),
        "Distance (km)": x["total_km"], "Total cost (€)": round(x["total_cost"], 2),
        "Average cost per truck (€)": round(x["total_cost"]/max(1,x["vehicles_used"]), 2),
        "Average cost per parcel (€)": round(x["total_cost"]/max(1,x["parcel_count"]), 2),
        "Combined score (€-equivalent)": round(x["objective_score"], 2),
    } for x in options]
    return best

def _size_balance_state(lockers, parcel_size):
    """Return a weighted utilisation-spread state for one compartment size."""
    size_key = parcel_size.lower()
    capacities = {int(l["id"]): int(l[f"{size_key}_capacity"]) for l in lockers}
    occupied = {int(l["id"]): int(l[f"{size_key}_occupied"]) for l in lockers}
    total_capacity = sum(capacities.values())
    total_occupied = sum(occupied.values())
    if total_capacity <= 0:
        return {"capacities": capacities, "occupied": occupied,
                "total_capacity": 0, "mean": 0.0, "sum_sq": 0.0,
                "spread": 0.0}

    mean = total_occupied / total_capacity
    sum_sq = sum(
        ((occupied[lid] - mean * cap) ** 2) / cap
        for lid, cap in capacities.items() if cap > 0
    )
    spread = (max(0.0, sum_sq / total_capacity) ** 0.5) * 100.0
    return {"capacities": capacities, "occupied": occupied,
            "total_capacity": total_capacity, "mean": mean,
            "sum_sq": sum_sq, "spread": spread}


def _marginal_balance_improvement(state, source_id, destination_id):
    """Calculate the change in weighted standard deviation in O(1)."""
    caps = state["capacities"]
    occ = state["occupied"]
    source_cap = caps.get(source_id, 0)
    destination_cap = caps.get(destination_id, 0)
    if source_cap <= 0 or destination_cap <= 0 or occ.get(source_id, 0) <= 0:
        return 0.0
    if occ[destination_id] >= destination_cap:
        return 0.0

    mean = state["mean"]
    source_before = ((occ[source_id] - mean * source_cap) ** 2) / source_cap
    source_after = ((occ[source_id] - 1 - mean * source_cap) ** 2) / source_cap
    destination_before = ((occ[destination_id] - mean * destination_cap) ** 2) / destination_cap
    destination_after = ((occ[destination_id] + 1 - mean * destination_cap) ** 2) / destination_cap
    new_sum_sq = max(0.0, state["sum_sq"] + source_after - source_before
                     + destination_after - destination_before)
    new_spread = (new_sum_sq / state["total_capacity"]) ** 0.5 * 100.0
    return state["spread"] - new_spread



def optimise_network_transfers(lockers, candidates, eur_per_balance_point=10.0):
    """
    Coordinated nationwide parcel redistribution.

    Improvements:
    - Groups and sorts candidate movements only once.
    - Calculates marginal network-score changes using only the
      source and destination lockers.
    - Re-evaluates every available lane after each accepted group.
    - Preserves regional weights, utilisation targets, cooldowns,
      compartment-size separation and direction restrictions.

    This is a greedy planning heuristic, not a guaranteed global optimum.
    """

    sizes = ("Small", "Medium", "Large")
    locker_by_id = {int(x["id"]): x for x in lockers}

    states = {
        size: _size_balance_state(lockers, size)
        for size in sizes
    }

    initial_spread = {
        size: states[size]["spread"]
        for size in sizes
    }

    initial_occupancy = {
        size: dict(states[size]["occupied"])
        for size in sizes
    }

    # --------------------------------------------------------
    # 1. REGIONAL WEIGHTS AND TARGET UTILISATION
    # --------------------------------------------------------

    def region_weight(locker):
        region = str(locker.get("region", "")).lower()

        if region == "riga" or region.startswith("riga "):
            return 1.35

        if "pier" in region:
            return 1.15

        return 1.0

    def target_for(locker):
        region = str(locker.get("region", "")).lower()

        if region == "riga" or region.startswith("riga "):
            return 0.85

        if "pier" in region:
            return 0.70

        return 0.60

    # Precalculate each locker's scoring parameters.
    scoring = {}

    for lid, locker in locker_by_id.items():
        scoring[lid] = {
            "weight": region_weight(locker),
            "target": target_for(locker),
        }

    def locker_score(size, locker_id, occupied):
        """
        Calculate the contribution of one locker to the network score.
        This is mathematically equivalent to its contribution in the
        original network_score function.
        """
        capacity = states[size]["capacities"].get(locker_id, 0)

        if capacity <= 0:
            return 0.0

        utilisation = occupied / capacity
        target = scoring[locker_id]["target"]
        weight = scoring[locker_id]["weight"]

        over = max(0.0, utilisation - target)
        under = max(0.0, target - utilisation)
        critical = max(0.0, utilisation - 0.92)

        return weight * capacity * (
            2.2 * over ** 2
            + 0.30 * under ** 2
            + 8.0 * critical ** 2
        )

    # --------------------------------------------------------
    # 2. PREPARE CANDIDATE MOVEMENTS
    # --------------------------------------------------------

    blocked_recent = {
        int(c["ParcelID"])
        for c in candidates
        if bool(c.get("RecentTransferWithin24Hours", False))
    }

    candidate_rows = [
        dict(c)
        for c in candidates
        if int(c["ParcelID"]) not in blocked_recent
    ]

    # Group candidates once. Each group represents one
    # source -> destination movement for one compartment size.
    grouped = {}

    for candidate in candidate_rows:
        size = str(candidate["ParcelSize"])

        if size not in states:
            continue

        src = int(candidate["SourceLockerID"])
        dst = int(candidate["DestinationLockerID"])

        if src == dst:
            continue

        grouped.setdefault((src, dst, size), []).append(candidate)

    # Sort each lane once by estimated transport cost.
    # The optimiser will consider progressively larger prefixes
    # of the cheapest available parcels.
    for rows in grouped.values():
        rows.sort(
            key=lambda x: float(
                x.get("EstimatedTransportCostEUR") or 0.0
            )
        )

    # --------------------------------------------------------
    # 3. INITIALISE THE PROJECTED NETWORK
    # --------------------------------------------------------

    selected = []
    used_parcels = set()

    # Each locker and compartment size can either send or receive,
    # but cannot do both in the same transport plan.
    directions = {}

    eur_per_balance_point = float(eur_per_balance_point)

    # --------------------------------------------------------
    # 4. ITERATIVELY SELECT THE BEST POSITIVE-VALUE GROUP
    # --------------------------------------------------------

    while True:

        best_group = None
        best_net = 0.0

        # Evaluate every available lane against the CURRENT
        # projected network state.
        for (src, dst, size), all_rows in grouped.items():

            # Respect the fixed direction of each locker.
            if (
                (src, size) in directions
                and directions[(src, size)] != "send"
            ):
                continue

            if (
                (dst, size) in directions
                and directions[(dst, size)] != "receive"
            ):
                continue

            state = states[size]

            source_capacity = state["capacities"].get(src, 0)
            destination_capacity = state["capacities"].get(dst, 0)

            source_occupied = state["occupied"].get(src, 0)
            destination_occupied = state["occupied"].get(dst, 0)

            if source_capacity <= 0 or destination_capacity <= 0:
                continue

            if source_occupied <= 0:
                continue

            destination_free = (
                destination_capacity - destination_occupied
            )

            if destination_free <= 0:
                continue

            source_utilisation = (
                source_occupied / source_capacity
            )

            # Preserve the original minimum group-size rule.
            minimum_group = (
                1 if source_utilisation >= 0.95 else 2
            )

            # Remove parcels already selected in another movement.
            rows = [
                row
                for row in all_rows
                if int(row["ParcelID"]) not in used_parcels
            ]

            max_group = min(
                len(rows),
                source_occupied,
                destination_free,
            )

            if max_group < minimum_group:
                continue

            # Only the source and destination contributions change.
            # This avoids recalculating the score across every locker.
            before_score = (
                locker_score(size, src, source_occupied)
                + locker_score(size, dst, destination_occupied)
            )

            lane_cost = 0.0
            lane_rows = []

            for n, row in enumerate(rows[:max_group], start=1):

                lane_rows.append(row)

                parcel_cost = float(
                    row.get("EstimatedTransportCostEUR") or 0.0
                )

                lane_cost += parcel_cost

                if n < minimum_group:
                    continue

                new_source_occupied = source_occupied - n
                new_destination_occupied = destination_occupied + n

                after_score = (
                    locker_score(size, src, new_source_occupied)
                    + locker_score(size, dst, new_destination_occupied)
                )

                improvement = before_score - after_score

                benefit = (
                    improvement * eur_per_balance_point
                )

                net = benefit - lane_cost

                if net > best_net:
                    best_net = net

                    best_group = (
                        src,
                        dst,
                        size,
                        list(lane_rows),
                        improvement,
                        benefit,
                        lane_cost,
                        net,
                    )

        # Stop when no remaining movement has positive net value.
        if best_group is None:
            break

        # ----------------------------------------------------
        # 5. APPLY THE BEST GROUP TO THE PROJECTED NETWORK
        # ----------------------------------------------------

        (
            src,
            dst,
            size,
            rows,
            improvement,
            benefit,
            cost,
            net,
        ) = best_group

        group_size = len(rows)

        for row in rows:

            parcel_id = int(row["ParcelID"])

            used_parcels.add(parcel_id)

            selected.append({
                **row,
                "BalanceImprovementPoints": improvement / group_size,
                "ModelledBalanceBenefitEUR": benefit / group_size,
                "EstimatedTransportCostEUR": float(
                    row.get("EstimatedTransportCostEUR") or 0.0
                ),
                "EstimatedNetBenefitEUR": net / group_size,
                "GroupSize": group_size,
                "Decision": "Recommended",
                "DecisionReason": (
                    f"Selected in coordinated group of {group_size} parcels"
                ),
            })

        directions[(src, size)] = "send"
        directions[(dst, size)] = "receive"

        state = states[size]

        state["occupied"][src] -= group_size
        state["occupied"][dst] += group_size

        # Update the raw balance metric for reporting.
        # This is done once per accepted group, not for every candidate.
        if state["total_capacity"] > 0:

            state["sum_sq"] = sum(
                (
                    (
                        state["occupied"][lid]
                        - state["mean"] * capacity
                    ) ** 2
                ) / capacity
                for lid, capacity in state["capacities"].items()
                if capacity > 0
            )

            state["spread"] = (
                max(
                    0.0,
                    state["sum_sq"] / state["total_capacity"]
                ) ** 0.5
            ) * 100.0

    # --------------------------------------------------------
    # 6. CLASSIFY ALL ORIGINAL CANDIDATE OPPORTUNITIES
    # --------------------------------------------------------

    selected_keys = {
        (
            int(x["ParcelID"]),
            int(x["SourceLockerID"]),
            int(x["DestinationLockerID"]),
            str(x["ParcelSize"]),
        )
        for x in selected
    }

    decisions = []

    for candidate in candidates:

        row = dict(candidate)

        parcel_id = int(candidate["ParcelID"])
        src = int(candidate["SourceLockerID"])
        dst = int(candidate["DestinationLockerID"])
        size = str(candidate["ParcelSize"])

        candidate_key = (parcel_id, src, dst, size)

        if candidate_key in selected_keys:

            row.update(
                Decision="Recommended",
                DecisionReason="Included in coordinated grouped plan",
            )

        elif bool(candidate.get("RecentTransferWithin24Hours", False)):

            row.update(
                Decision="Blocked",
                DecisionReason="Parcel transferred within the last 24 hours",
            )

        elif (
            (src, size) in directions
            and directions[(src, size)] != "send"
        ):

            row.update(
                Decision="Blocked",
                DecisionReason=(
                    "Locker is receiving this compartment size in the plan"
                ),
            )

        elif (
            (dst, size) in directions
            and directions[(dst, size)] != "receive"
        ):

            row.update(
                Decision="Blocked",
                DecisionReason=(
                    "Locker is sending this compartment size in the plan"
                ),
            )

        else:

            row.update(
                Decision="Not selected",
                DecisionReason=(
                    "Not part of the best positive-value grouped movement"
                ),
            )

        decisions.append(row)

    # --------------------------------------------------------
    # 7. FINAL PROJECTED OCCUPANCY AND SPREAD
    # --------------------------------------------------------

    final_occupancy = {
        size: dict(states[size]["occupied"])
        for size in sizes
    }

    final_spread = {
        size: states[size]["spread"]
        for size in sizes
    }

    locker_changes = []

    for locker in lockers:

        locker_id = int(locker["id"])

        item = {
            "Locker": locker["name"],
            "Region": locker["region"],
        }

        for size in sizes:

            before = initial_occupancy[size][locker_id]
            after = final_occupancy[size][locker_id]
            capacity = states[size]["capacities"][locker_id]

            item[f"{size} before %"] = (
                round(100 * before / capacity, 1)
                if capacity else 0
            )

            item[f"{size} after %"] = (
                round(100 * after / capacity, 1)
                if capacity else 0
            )

            item[f"{size} change"] = after - before

        locker_changes.append(item)

    # --------------------------------------------------------
    # 8. CONSOLIDATE MOVEMENTS FOR MAPS AND OPERATIONAL VIEWS
    # --------------------------------------------------------

    lanes = {}

    for item in selected:

        key = (
            int(item["SourceLockerID"]),
            int(item["DestinationLockerID"]),
            str(item["ParcelSize"]),
        )

        lanes.setdefault(key, []).append(item)

    movements = []

    for (src, dst, size), parcels in lanes.items():

        first = parcels[0]

        movements.append({
            "SourceLockerID": src,
            "DestinationLockerID": dst,
            "ParcelSize": size,
            "SourceLockerName": first["SourceLockerName"],
            "DestinationLockerName": first["DestinationLockerName"],
            "ParcelCount": len(parcels),
            "ParcelCodes": [
                item["ParcelCode"] for item in parcels
            ],
            "TotalCostEUR": sum(
                float(item["EstimatedTransportCostEUR"] or 0)
                for item in parcels
            ),
            "EstimatedRoadDistanceKm": float(
                first.get("EstimatedRoadDistanceKm") or 0
            ),
        })

    # --------------------------------------------------------
    # 9. RETURN THE ORIGINAL OUTPUT STRUCTURE
    # --------------------------------------------------------

    return {
        "recommendations": selected,
        "opportunities": decisions,
        "initial_spread": initial_spread,
        "final_spread": final_spread,
        "initial_occupancy": initial_occupancy,
        "final_occupancy": final_occupancy,
        "locker_changes": locker_changes,
        "movements": movements,
        "total_cost": sum(
            float(x["EstimatedTransportCostEUR"] or 0)
            for x in selected
        ),
        "total_benefit": sum(
            float(x["ModelledBalanceBenefitEUR"] or 0)
            for x in selected
        ),
        "total_net_benefit": sum(
            float(x["EstimatedNetBenefitEUR"] or 0)
            for x in selected
        ),
        "balance_value_eur": eur_per_balance_point,
    }


def add_transfer_arrow(map_object, source, destination, lane, colour):
    """Draw one interactive source-to-destination transfer lane.

    Hovering either the line or its arrow shows size counts. Clicking either
    opens a scrollable detail popup with parcel codes grouped by size.
    """
    a = [float(source["lat"]), float(source["lon"])]
    b = [float(destination["lat"]), float(destination["lon"])]
    sizes = ("Small", "Medium", "Large")
    counts = {size: int(lane.get("counts", {}).get(size, 0)) for size in sizes}
    parcels = {size: list(lane.get("parcels", {}).get(size, [])) for size in sizes}
    total = sum(counts.values())

    source_label = f"{lane['source_name']} ({lane['source_code']})"
    destination_label = f"{lane['destination_name']} ({lane['destination_code']})"
    summary = " | ".join(f"{size}: {counts[size]}" for size in sizes)

    # Compact hover card: source/destination plus counts, without a long ID list.
    tooltip_html = (
        "<div style='font-family:Arial,sans-serif;font-size:12px;line-height:1.45'>"
        f"<b>{html.escape(source_label)}</b> &rarr; "
        f"<b>{html.escape(destination_label)}</b><br>"
        f"<b>{total} parcels</b><br>{summary}</div>"
    )

    # Click detail: explicit origin and destination, then parcel IDs grouped by size.
    group_sections = []
    for size in sizes:
        codes = parcels[size]
        code_list = "<br>".join(html.escape(str(code)) for code in codes)
        if not code_list:
            code_list = "<span style='color:#777'>No parcels</span>"
        group_sections.append(
            f"<div style='margin:9px 0;padding:7px;border-left:4px solid {lane.get('colours', {}).get(size, '#64748b')};background:#f7f7f7'>"
            f"<b>{size} parcels ({counts[size]})</b><br>{code_list}</div>"
        )
    popup_html = (
        "<div style='font-family:Arial,sans-serif;color:#222;min-width:260px;max-width:360px'>"
        f"<div style='font-size:14px'><b>Transfer origin</b><br>{html.escape(source_label)}</div>"
        f"<div style='margin:5px 0;color:#555'>&darr; Transfer destination</div>"
        f"<div style='font-size:14px'><b>{html.escape(destination_label)}</b></div>"
        f"<hr><b>{total} parcels in this transfer</b>"
        f"<div style='max-height:260px;overflow-y:auto'>{''.join(group_sections)}</div></div>"
    )

    tooltip = folium.Tooltip(tooltip_html, sticky=True, direction="top", opacity=0.97)
    popup = folium.Popup(popup_html, max_width=400, max_height=390)

    # Wide enough to hover/click easily; popup is also attached to the arrow marker.
    folium.PolyLine(
        [a, b], color=colour, weight=7, opacity=0.9,
        tooltip=tooltip, popup=popup,
    ).add_to(map_object)

    # Place a prominent arrow at the geographic midpoint and rotate it to the
    # source-to-destination bearing in Leaflet's screen coordinate system.
    lat_mid = (a[0] + b[0]) / 2
    lon_mid = (a[1] + b[1]) / 2
    dx = (b[1] - a[1]) * math.cos(math.radians(lat_mid))
    dy = -(b[0] - a[0])
    angle = math.degrees(math.atan2(dy, dx))
    arrow_html = (
        "<div style='width:34px;height:34px;display:flex;align-items:center;justify-content:center;"
        f"font-size:30px;font-weight:900;color:{colour};"
        "-webkit-text-stroke:1px white;text-shadow:0 0 4px white,0 0 7px white;"
        f"transform:rotate({angle:.1f}deg);transform-origin:center;cursor:pointer;pointer-events:auto'>➤</div>"
    )
    folium.Marker(
        [lat_mid, lon_mid],
        icon=folium.DivIcon(html=arrow_html, icon_size=(34, 34), icon_anchor=(17, 17)),
        tooltip=folium.Tooltip(tooltip_html, sticky=True, direction="top", opacity=0.97),
        popup=folium.Popup(popup_html, max_width=400, max_height=390),
        z_index_offset=2000,
    ).add_to(map_object)

def make_transfer_plan_map(lockers, plan, show_arrows=True):
    """Map final recommended lanes only; lines are schematic straight-line links."""
    m = make_locker_map(lockers, plan=plan)
    by_id = {int(x["id"]): x for x in lockers}
    if show_arrows:
        palette = {"Small": "#2a9d8f", "Medium": "#e9a23b", "Large": "#d1495b"}
        for lane in plan.get("movements", []):
            src = by_id.get(lane["SourceLockerID"]); dst = by_id.get(lane["DestinationLockerID"])
            if not src or not dst:
                continue
            parcels = "<br>".join(html.escape(x) for x in lane["ParcelCodes"])
            popup = (f"<b>{html.escape(lane['SourceLockerName'])} → "
                     f"{html.escape(lane['DestinationLockerName'])}</b><br>"
                     f"{lane['ParcelCount']} {lane['ParcelSize']} parcel(s)<br>"
                     f"Distance: {lane['EstimatedRoadDistanceKm']:.1f} road km<br>"
                     f"Estimated cost: €{lane['TotalCostEUR']:.2f}<hr>{parcels}")
            folium.PolyLine([[src["lat"], src["lon"]], [dst["lat"], dst["lon"]]],
                color=palette.get(lane["ParcelSize"], "#555"), weight=4, opacity=.8,
                tooltip=f"{lane['ParcelCount']} {lane['ParcelSize']} parcel(s)",
                popup=folium.Popup(popup, max_width=360)).add_to(m)
            folium.Marker([dst["lat"], dst["lon"]], icon=folium.DivIcon(
                html=f'<div style="font-size:20px;color:{palette.get(lane["ParcelSize"], "#555")};">➤</div>',
                icon_size=(18,18), icon_anchor=(9,9))).add_to(m)
    if lockers:
        m.fit_bounds([[x["lat"], x["lon"]] for x in lockers])
    return m


def execute_parcel_transfer(parcel_id, destination_locker_id, manager_override=False):
    """Execute one transfer atomically, rechecking parcel and capacity inside SQL."""
    connection = pyodbc.connect(get_connection_string(), autocommit=False)
    try:
        cursor = connection.cursor()
        cursor.execute("SET XACT_ABORT ON;")

        # Lock the parcel so two users cannot move it at the same time.
        cursor.execute("""
            SELECT ParcelID, ParcelCode, ParcelSize, CurrentLockerID
            FROM dbo.Parcel WITH (UPDLOCK, HOLDLOCK)
            WHERE ParcelID = ? AND Status = 'AtLocker';
        """, parcel_id)
        parcel = cursor.fetchone()
        if parcel is None:
            raise ValueError("This parcel is no longer available at its original locker. Refresh and try again.")

        source_id = int(parcel.CurrentLockerID)
        parcel_size = str(parcel.ParcelSize)

        cursor.execute("""
            SELECT TOP 1 EventAt
            FROM dbo.ParcelMovement WITH (UPDLOCK, HOLDLOCK)
            WHERE ParcelID = ? AND EventType = 'Transferred'
            ORDER BY EventAt DESC, MovementID DESC;
        """, parcel_id)
        last_transfer = cursor.fetchone()
        if last_transfer is not None and not manager_override:
            cursor.execute("SELECT DATEADD(HOUR, -24, SYSDATETIME());")
            cooldown_start = cursor.fetchone()[0]
            if last_transfer.EventAt >= cooldown_start:
                raise ValueError(
                    "This parcel was transferred within the last 24 hours. "
                    "A manager override is required to move it again."
                )

        if source_id == int(destination_locker_id):
            raise ValueError("The destination must be different from the source locker.")

        # Lock the destination locker row. Transfers to the same destination are serialised.
        cursor.execute("""
            SELECT LockerID, Latitude, Longitude
            FROM dbo.Locker WITH (UPDLOCK, HOLDLOCK)
            WHERE LockerID = ? AND IsActive = 1;
        """, destination_locker_id)
        destination = cursor.fetchone()
        if destination is None:
            raise ValueError("The destination locker is inactive or no longer exists.")

        cursor.execute("""
            SELECT
                COALESCE(SUM(c.TotalCompartments), 0) AS Capacity,
                (SELECT COUNT(*) FROM dbo.Parcel p WITH (UPDLOCK, HOLDLOCK)
                 WHERE p.CurrentLockerID = ? AND p.ParcelSize = ? AND p.Status = 'AtLocker') AS Occupied
            FROM dbo.LockerCompartment c
            WHERE c.LockerID = ? AND c.CompartmentSize = ?;
        """, destination_locker_id, parcel_size, destination_locker_id, parcel_size)
        capacity_row = cursor.fetchone()
        capacity = int(capacity_row.Capacity or 0)
        occupied = int(capacity_row.Occupied or 0)
        if capacity <= occupied:
            raise ValueError(
                f"Destination has no available {parcel_size.lower()} compartment "
                f"({occupied}/{capacity} occupied)."
            )

        cursor.execute("""
            SELECT Latitude, Longitude
            FROM dbo.Locker
            WHERE LockerID = ? AND IsActive = 1;
        """, source_id)
        source = cursor.fetchone()
        if source is None or any(v is None for v in [source.Latitude, source.Longitude,
                                                      destination.Latitude, destination.Longitude]):
            raise ValueError("Source or destination coordinates are missing; transfer estimate cannot be calculated.")

        straight_km = haversine_km(
            source.Latitude, source.Longitude,
            destination.Latitude, destination.Longitude
        )
        # Use the database's illustrative cost assumptions where available.
        try:
            cursor.execute("""
                SELECT RoadDistanceFactor, CostPerKmEUR, FixedHandlingCostEUR
                FROM dbo.TransportCostSettings WHERE SettingID = 1;
            """)
            settings = cursor.fetchone()
            factor = float(settings.RoadDistanceFactor) if settings else 1.30
            cost_per_km = float(settings.CostPerKmEUR) if settings else 0.45
            fixed_cost = float(settings.FixedHandlingCostEUR) if settings else 1.50
        except pyodbc.Error:
            factor, cost_per_km, fixed_cost = 1.30, 0.45, 1.50

        road_km = straight_km * factor
        estimated_cost = fixed_cost + road_km * cost_per_km
        cursor.execute("SELECT SYSDATETIME();")
        event_time = cursor.fetchone()[0]

        cursor.execute("""
            SELECT CASE WHEN COL_LENGTH('dbo.ParcelMovement', 'StraightLineDistanceKm') IS NOT NULL
                        AND COL_LENGTH('dbo.ParcelMovement', 'EstimatedRoadDistanceKm') IS NOT NULL
                        AND COL_LENGTH('dbo.ParcelMovement', 'EstimatedTransportCostEUR') IS NOT NULL
                        THEN 1 ELSE 0 END;
        """)
        has_cost_columns = bool(cursor.fetchone()[0])

        cursor.execute("""
            UPDATE dbo.Parcel
            SET CurrentLockerID = ?, LastStatusAt = ?
            WHERE ParcelID = ? AND Status = 'AtLocker' AND CurrentLockerID = ?;
        """, destination_locker_id, event_time, parcel_id, source_id)
        if cursor.rowcount != 1:
            raise ValueError("The parcel changed while the transfer was being processed. No transfer was saved.")

        note = "Manager-confirmed prototype transfer; distance and cost are illustrative estimates."
        if has_cost_columns:
            cursor.execute("""
                INSERT INTO dbo.ParcelMovement
                    (ParcelID, FromLockerID, ToLockerID, EventType, EventAt, Notes,
                     StraightLineDistanceKm, EstimatedRoadDistanceKm, EstimatedTransportCostEUR)
                VALUES (?, ?, ?, 'Transferred', ?, ?, ?, ?, ?);
            """, parcel_id, source_id, destination_locker_id, event_time, note,
                 straight_km, road_km, estimated_cost)
        else:
            cursor.execute("""
                INSERT INTO dbo.ParcelMovement
                    (ParcelID, FromLockerID, ToLockerID, EventType, EventAt, Notes)
                VALUES (?, ?, ?, 'Transferred', ?, ?);
            """, parcel_id, source_id, destination_locker_id, event_time, note)

        connection.commit()
        return {
            "parcel_code": str(parcel.ParcelCode),
            "size": parcel_size,
            "straight_km": straight_km,
            "road_km": road_km,
            "estimated_cost": estimated_cost,
            "cost_saved": has_cost_columns,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@st.cache_data(ttl=30, show_spinner=False)
def load_recent_movements():
    query = """
    SELECT TOP 20
        pm.MovementID, pm.EventAt, pm.EventType, p.ParcelCode, p.ParcelSize,
        src.LockerName AS FromLocker, dst.LockerName AS ToLocker,
        pm.EstimatedRoadDistanceKm, pm.EstimatedTransportCostEUR
    FROM dbo.ParcelMovement pm
    JOIN dbo.Parcel p ON p.ParcelID = pm.ParcelID
    LEFT JOIN dbo.Locker src ON src.LockerID = pm.FromLockerID
    LEFT JOIN dbo.Locker dst ON dst.LockerID = pm.ToLockerID
    ORDER BY pm.EventAt DESC, pm.MovementID DESC;
    """
    with pyodbc.connect(get_connection_string()) as connection:
        cursor = connection.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ============================================================
# 4. DATA VALIDATION
# ============================================================

def validate_lockers(lockers):
    errors = []
    warnings = []

    for locker in lockers:
        name = locker["name"]

        occupied_by_size = (
            locker["small_occupied"]
            + locker["medium_occupied"]
            + locker["large_occupied"]
        )
        capacity_by_size = (
            locker["small_capacity"]
            + locker["medium_capacity"]
            + locker["large_capacity"]
        )

        if occupied_by_size != locker["occupied"]:
            errors.append(
                f"{name}: total occupancy does not match S/M/L occupancy."
            )

        if capacity_by_size != locker["capacity"]:
            errors.append(
                f"{name}: total capacity does not match S/M/L capacity."
            )

        size_checks = [
            ("Small", locker["small_occupied"], locker["small_capacity"]),
            ("Medium", locker["medium_occupied"], locker["medium_capacity"]),
            ("Large", locker["large_occupied"], locker["large_capacity"]),
        ]

        for size_name, occupied, capacity in size_checks:
            if occupied > capacity:
                errors.append(
                    f"{name}: {size_name} occupancy exceeds capacity "
                    f"({occupied}/{capacity})."
                )
            if occupied < 0 or capacity < 0:
                errors.append(f"{name}: negative {size_name} values.")

        if locker["occupied"] > locker["capacity"]:
            errors.append(f"{name}: overall occupancy exceeds capacity.")

        if locker["capacity"] > 0:
            calculated = locker["occupied"] / locker["capacity"] * 100
            if abs(locker["utilisation"] - calculated) > 0.11:
                errors.append(f"{name}: utilisation percentage is inconsistent.")
        elif locker["occupied"] > 0:
            errors.append(f"{name}: parcels exist despite zero capacity.")
        else:
            warnings.append(f"{name}: zero total capacity.")

        lat = locker["lat"]
        lon = locker["lon"]
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            errors.append(f"{name}: invalid geographical coordinates.")

    return errors, warnings


# ============================================================
# 5. MAP HELPERS
# ============================================================

def occupancy_colour(utilisation):
    if utilisation >= 85:
        return "#dc3545"
    if utilisation >= 70:
        return "#f0ad4e"
    return "#28a745"


def make_occupancy_ring(occupied, capacity):
    if capacity <= 0:
        percentage = 0
        display = "--"
    else:
        percentage = min(100, max(0, occupied / capacity * 100))
        display = f"{percentage:.0f}%"

    colour = occupancy_colour(percentage)

    return f"""
    <div style="
        width:50px;height:50px;border-radius:50%;
        background:conic-gradient({colour} {percentage}%,
        #d9dee5 {percentage}% 100%);
        display:flex;align-items:center;justify-content:center;">
        <div style="
            width:38px;height:38px;border-radius:50%;background:white;
            display:flex;align-items:center;justify-content:center;
            font-size:11px;font-weight:bold;color:#222;">
            {display}
        </div>
    </div>
    """


def make_locker_map(lockers, plan=None):
    map_object = folium.Map(
        location=[56.95, 24.6],
        zoom_start=7,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
        dragging=True,
        touchZoom=True,
        doubleClickZoom=True,
        scrollWheelZoom=True,
    )

    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri",
        name="Street map",
        overlay=False,
        control=True,
    ).add_to(map_object)

    for locker in lockers:
        lid = int(locker["id"])
        if plan is not None:
            current = {size: int(plan["initial_occupancy"][size][lid]) for size in ("Small", "Medium", "Large")}
            projected = {size: int(plan["final_occupancy"][size][lid]) for size in ("Small", "Medium", "Large")}
            caps = {size: int(locker[f"{size.lower()}_capacity"]) for size in ("Small", "Medium", "Large")}
            occupied_after = sum(projected.values())
            total_capacity = sum(caps.values())
            utilisation = 100 * occupied_after / total_capacity if total_capacity else 0
            display_occupied = occupied_after
        else:
            current = projected = {size: int(locker[f"{size.lower()}_occupied"]) for size in ("Small", "Medium", "Large")}
            caps = {size: int(locker[f"{size.lower()}_capacity"]) for size in ("Small", "Medium", "Large")}
            utilisation = float(locker["utilisation"])
            display_occupied = int(locker["occupied"])
        utilisation = min(100, max(0, utilisation))
        colour = occupancy_colour(utilisation)

        name = html.escape(locker["name"])
        region = html.escape(locker["region"])
        code = html.escape(locker["code"])

        small_ring = make_occupancy_ring(projected["Small"], caps["Small"])
        medium_ring = make_occupancy_ring(projected["Medium"], caps["Medium"])
        large_ring = make_occupancy_ring(projected["Large"], caps["Large"])
        before_after = ""
        if plan is not None:
            before_after = "<div style='margin-top:8px;font-size:12px;color:#555;'><b>Before → projected:</b> " + " | ".join(
                f"{size[0]} {current[size]}/{caps[size]} → {projected[size]}/{caps[size]}" for size in ("Small", "Medium", "Large")
            ) + "</div>"

        popup_html = f"""
        <div style="min-width:280px;font-family:Arial,sans-serif;
                    color:#222;padding:5px;">
            <h3 style="margin:0 0 8px 0;font-size:18px;">{name}</h3>
            <div style="font-size:13px;color:#666;line-height:1.6;">
                <b>Region:</b> {region}<br>
                <b>Locker code:</b> {code}
            </div>
            <div style="margin-top:15px;margin-bottom:15px;padding:12px;
                        background:#f3f5f7;border-radius:8px;">
                <div style="font-size:12px;color:#666;font-weight:bold;">
                    OVERALL OCCUPANCY
                </div>
                <div style="font-size:25px;font-weight:bold;color:{colour};
                            margin-top:4px;">
                    {display_occupied} / {locker['capacity']}
                    ({utilisation:.1f}%)
                </div>
            </div>
            <div style="font-size:13px;font-weight:bold;
                        margin-bottom:15px;text-align:center;">
                COMPARTMENT OCCUPANCY
            </div>
            <div style="display:flex;justify-content:space-around;
                        text-align:center;gap:8px;">
                <div style="flex:1;">
                    {small_ring}
                    <b>SMALL</b><br>
                    <span style="font-size:13px;color:#666;">
                        {projected['Small']} / {caps['Small']}
                    </span>
                </div>
                <div style="flex:1;">
                    {medium_ring}
                    <b>MEDIUM</b><br>
                    <span style="font-size:13px;color:#666;">
                        {projected['Medium']} / {caps['Medium']}
                    </span>
                </div>
                <div style="flex:1;">
                    {large_ring}
                    <b>LARGE</b><br>
                    <span style="font-size:13px;color:#666;">
                        {projected['Large']} / {caps['Large']}
                    </span>
                </div>
            </div>
            {before_after}
        </div>
        """

        marker_html = f"""
        <div style="
            width:38px;height:38px;border-radius:50%;
            background:conic-gradient({colour} {utilisation}%,
            #d9dee5 {utilisation}% 100%);
            border:1px solid #777;
            display:flex;align-items:center;justify-content:center;
            box-shadow:0 1px 4px rgba(0,0,0,0.3);">
            <div style="
                width:28px;height:28px;border-radius:50%;background:white;
                display:flex;align-items:center;justify-content:center;
                font-size:10px;font-weight:bold;color:#222;">
                {utilisation:.0f}%
            </div>
        </div>
        """

        folium.Marker(
            location=[locker["lat"], locker["lon"]],
            tooltip=f"{name}: {utilisation:.1f}%",
            popup=folium.Popup(popup_html, max_width=340),
            icon=folium.DivIcon(
                html=marker_html,
                icon_size=(44, 44),
                icon_anchor=(22, 22),
            ),
        ).add_to(map_object)

    legend_html = """
    <div style="
        position:fixed;bottom:25px;left:25px;z-index:9999;
        background:white;padding:12px 15px;border:1px solid #aaa;
        border-radius:7px;box-shadow:0 1px 5px rgba(0,0,0,0.25);
        font-family:Arial,sans-serif;font-size:12px;">
        <b>Locker occupancy</b><br><br>
        <span style="color:#28a745;">●</span> Low: below 70%<br>
        <span style="color:#f0ad4e;">●</span> Medium: 70–84.9%<br>
        <span style="color:#dc3545;">●</span> High: 85% or above<br>
        <hr style="margin:7px 0;">
        Click a marker to see Small, Medium and Large occupancy.
    </div>
    """
    map_object.get_root().html.add_child(folium.Element(legend_html))

    folium.LayerControl(collapsed=True).add_to(map_object)

    if lockers:
        map_object.fit_bounds([
            [locker["lat"], locker["lon"]] for locker in lockers
        ])

    return map_object


# ============================================================
# 6. APPLICATION HEADER AND NAVIGATION
# ============================================================

st.title("📦 Parcel Network Optimiser")
st.caption(
    "Latvijas Pasts | Parcel capacity monitoring and route optimisation"
)

page = st.sidebar.radio(
    "Navigation",
    [
        "Network Overview",
        "Parcel Management",
        "Route Optimisation",
        "Capacity Scenarios",
    ],
)

if st.sidebar.button("Refresh database data", use_container_width=True):
    load_lockers.clear()
    load_parcels.clear()
    load_recent_movements.clear()
    load_transfer_recommendations.clear()
    st.session_state.pop("network_optimiser_plan", None)
    st.session_state.pop("network_optimiser_value", None)
    st.session_state.pop("network_optimiser_generated_at", None)
    st.rerun()


# ============================================================
# 7. LOAD DATA
# ============================================================

try:
    lockers = load_lockers()
except Exception as error:
    st.error("Could not retrieve locker data from Azure SQL.")
    st.exception(error)
    st.info(
        "Check that your .env file is in the project root, "
        "the database is reachable, and ODBC Driver 18 is installed."
    )
    st.stop()

validation_errors, validation_warnings = validate_lockers(lockers)

if validation_errors:
    st.error(
        f"Data validation found {len(validation_errors)} issue(s). "
        "The map is shown for inspection, but operational actions are disabled."
    )
    with st.expander("View validation issues"):
        for issue in validation_errors:
            st.write(f"- {issue}")
else:
    st.success(
        f"Connected to Azure SQL. {len(lockers)} active lockers retrieved. "
        "Data validation passed."
    )

if validation_warnings:
    with st.expander("Data validation warnings"):
        for warning in validation_warnings:
            st.write(f"- {warning}")


# ============================================================
# 8. NETWORK OVERVIEW
# ============================================================

if page == "Network Overview":
    total_lockers = len(lockers)
    total_occupied = sum(locker["occupied"] for locker in lockers)
    total_capacity = sum(locker["capacity"] for locker in lockers)
    network_utilisation = (
        total_occupied / total_capacity * 100 if total_capacity else 0
    )
    critical_count = sum(
        locker["utilisation"] >= 85 for locker in lockers
    )
    high_count = sum(
        70 <= locker["utilisation"] < 85 for locker in lockers
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Active lockers", total_lockers)
    col2.metric("Occupied parcels", f"{total_occupied:,}")
    col3.metric("Total capacity", f"{total_capacity:,}")
    col4.metric("Network utilisation", f"{network_utilisation:.1f}%")
    col5.metric("High / critical", f"{high_count} / {critical_count}")

    st.divider()
    st.subheader("National locker network")

    # If a nationwide transfer plan has been calculated, let the user inspect
    # the live database snapshot and the simulated post-transfer network.
    network_plan = st.session_state.get("network_optimiser_plan")
    network_plan_valid = bool(
        network_plan
        and st.session_state.get("network_optimiser_value") is not None
    )
    if network_plan_valid:
        st.caption("A proposed transfer plan is available. Switching to projected view changes only the map simulation; Azure SQL is not modified.")
        map_view = st.radio(
            "Network map view",
            ["Current database", "Projected after transfers", "Projected with before/after details"],
            horizontal=True,
            key="network_overview_map_view",
        )
    else:
        map_view = "Current database"
        st.caption("Run the nationwide optimiser in Parcel Management to preview how its proposed transfers would change locker utilisation here.")

    all_regions = sorted({locker["region"] for locker in lockers})
    selected_regions = st.multiselect(
        "Filter by region",
        options=all_regions,
        default=all_regions,
    )

    status_options = [
        "All utilisation levels",
        "Low (<70%)",
        "High (70–84.9%)",
        "Critical (85%+)",
    ]
    selected_status = st.selectbox(
        "Filter by occupancy",
        status_options,
    )

    filtered_lockers = [
        locker for locker in lockers
        if locker["region"] in selected_regions
    ]

    if selected_status == "Low (<70%)":
        filtered_lockers = [
            locker for locker in filtered_lockers
            if locker["utilisation"] < 70
        ]
    elif selected_status == "High (70–84.9%)":
        filtered_lockers = [
            locker for locker in filtered_lockers
            if 70 <= locker["utilisation"] < 85
        ]
    elif selected_status == "Critical (85%+)":
        filtered_lockers = [
            locker for locker in filtered_lockers
            if locker["utilisation"] >= 85
        ]

    st.caption(
        f"Displaying {len(filtered_lockers)} of {total_lockers} active lockers. "
        "Click a map marker to inspect its compartment occupancy."
    )

    if filtered_lockers:
        show_projection = network_plan_valid and map_view != "Current database"
        locker_map = make_locker_map(filtered_lockers, plan=network_plan if show_projection else None)
        if show_projection and map_view == "Projected after transfers":
            st.caption("Markers show projected occupancy after all recommended transfers. Open a marker for the projected Small, Medium and Large counts.")
        elif show_projection:
            st.caption("Markers show projected occupancy. Marker popups compare current and projected Small, Medium and Large compartment counts.")
        st_folium(
            locker_map,
            use_container_width=True,
            height=650,
            returned_objects=[],
            key=f"locker_network_map_{map_view.replace(' ', '_')}",
        )
    else:
        st.warning("No lockers match the selected filters.")

    st.subheader("Locker details")

    locker_names = sorted(locker["name"] for locker in filtered_lockers)
    if locker_names:
        selected_locker_name = st.selectbox(
            "Select a locker",
            locker_names,
        )
        selected_locker = next(
            locker for locker in filtered_lockers
            if locker["name"] == selected_locker_name
        )

        st.markdown(f"### {selected_locker['name']}")
        st.caption(
            f"{selected_locker['region']} | "
            f"Locker code: {selected_locker['code']}"
        )

        detail1, detail2, detail3 = st.columns(3)
        detail1.metric(
            "Overall occupancy",
            f"{selected_locker['occupied']} / {selected_locker['capacity']}",
            f"{selected_locker['utilisation']:.1f}%",
        )
        detail2.metric(
            "Available compartments",
            selected_locker["capacity"] - selected_locker["occupied"],
        )
        detail3.metric(
            "Utilisation status",
            (
                "Critical" if selected_locker["utilisation"] >= 85
                else "High" if selected_locker["utilisation"] >= 70
                else "Low"
            ),
        )

        size_cols = st.columns(3)
        size_data = [
            ("Small", "small_occupied", "small_capacity"),
            ("Medium", "medium_occupied", "medium_capacity"),
            ("Large", "large_occupied", "large_capacity"),
        ]

        for column, (label, occupied_key, capacity_key) in zip(
            size_cols, size_data
        ):
            occupied = selected_locker[occupied_key]
            capacity = selected_locker[capacity_key]
            percentage = occupied / capacity if capacity else 0

            with column:
                st.markdown(f"**{label} compartments**")
                st.progress(min(1.0, percentage))
                st.write(f"{occupied} / {capacity} occupied")
                st.caption(f"{percentage:.1%} utilisation")

    st.subheader("Network locker table")
    table_rows = []
    for locker in filtered_lockers:
        table_rows.append({
            "Locker": locker["name"],
            "Region": locker["region"],
            "Occupied": locker["occupied"],
            "Capacity": locker["capacity"],
            "Available": locker["capacity"] - locker["occupied"],
            "Utilisation (%)": locker["utilisation"],
        })

    st.dataframe(
        table_rows,
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "Data source: synthetic Azure SQL project dataset. "
        "Figures represent the current database snapshot, not live Latvijas Pasts operations."
    )


# ============================================================
# 9. PLACEHOLDER SECTIONS FOR THE NEXT MILESTONES
# ============================================================

elif page == "Parcel Management":
    st.subheader("Parcel Management")
    st.caption("Review parcels currently stored in lockers, assess transfer opportunities, and confirm individual transfers.")

    if "last_transfer_result" in st.session_state:
        result = st.session_state.pop("last_transfer_result")
        st.success(
            f"Transfer completed: {result['parcel_code']} moved successfully. "
            f"Estimated road distance: {result['road_km']:.2f} km; "
            f"illustrative transport cost: €{result['estimated_cost']:.2f}."
        )

    try:
        parcels = load_parcels()
    except Exception as error:
        parcels = []
        st.error("Could not retrieve parcel records. Check that dbo.Parcel exists and is accessible.")
        st.exception(error)

    if parcels:
        parcel_col1, parcel_col2, parcel_col3 = st.columns(3)
        parcel_col1.metric("Parcels currently at lockers", len(parcels))
        parcel_col2.metric("Small", sum(p["size"] == "Small" for p in parcels))
        parcel_col3.metric("Medium / Large", sum(p["size"] in ("Medium", "Large") for p in parcels))

        st.subheader("1. Manual parcel transfer")
        locker_by_id = {locker["id"]: locker for locker in lockers}
        parcel_by_id = {parcel["id"]: parcel for parcel in parcels}
        parcel_ids = [p["id"] for p in parcels]
        # A previously selected parcel may have been transferred and disappeared
        # from the options. Clear stale widget state before constructing the selectbox.
        if st.session_state.get("manual_parcel_id") not in parcel_ids:
            st.session_state.pop("manual_parcel_id", None)

        selected_parcel_id = st.selectbox(
            "Select a parcel",
            options=parcel_ids,
            format_func=lambda pid: (
                f"{parcel_by_id[pid]['code']} | {parcel_by_id[pid]['size']} | "
                f"{parcel_by_id[pid]['locker_name']}"
            ),
            key="manual_parcel_id",
        )
        selected_parcel = parcel_by_id[selected_parcel_id]
        source_locker = locker_by_id.get(selected_parcel["locker_id"])
        destination_options = [
            locker["id"] for locker in lockers
            if locker["id"] != selected_parcel["locker_id"]
            and locker[f"{selected_parcel['size'].lower()}_capacity"]
                > locker[f"{selected_parcel['size'].lower()}_occupied"]
        ]
        if st.session_state.get("manual_destination_id") not in destination_options:
            st.session_state.pop("manual_destination_id", None)

        if not destination_options:
            st.warning(f"No other locker currently has an available {selected_parcel['size'].lower()} compartment.")
        else:
            selected_destination_id = st.selectbox(
                "Destination locker",
                options=destination_options,
                format_func=lambda lid: (
                    f"{locker_by_id[lid]['name']} | "
                    f"{locker_by_id[lid]['region']} | "
                    f"{locker_by_id[lid][selected_parcel['size'].lower() + '_occupied']}/"
                    f"{locker_by_id[lid][selected_parcel['size'].lower() + '_capacity']} "
                    f"{selected_parcel['size'].lower()} compartments occupied"
                ),
                key="manual_destination_id",
            )

            manager_override = st.checkbox(
                "Manager override: bypass the 24-hour transfer cooldown",
                value=False,
                help="Use only when an authorised manager has reviewed the operational reason. "
                     "This prototype toggle is not yet protected by application role-based access."
            )
            if manager_override:
                st.warning("Cooldown override enabled. The prototype does not yet enforce manager login or roles.")

            if st.button("Preview transfer", type="primary", disabled=bool(validation_errors)):
                destination_locker = locker_by_id[selected_destination_id]
                size_key = selected_parcel["size"].lower()
                available = destination_locker[f"{size_key}_capacity"] - destination_locker[f"{size_key}_occupied"]
                if available <= 0:
                    st.error("The destination no longer appears to have matching capacity. Refresh the data.")
                    st.session_state.pop("pending_transfer", None)
                else:
                    st.session_state["pending_transfer"] = {
                        "parcel_id": selected_parcel_id,
                        "destination_id": selected_destination_id,
                        "parcel_code": selected_parcel["code"],
                        "size": selected_parcel["size"],
                        "source_name": selected_parcel["locker_name"],
                        "destination_name": destination_locker["name"],
                        "available": available,
                        "manager_override": manager_override,
                    }

            pending = st.session_state.get("pending_transfer")
            if pending:
                st.divider()
                st.markdown("#### Transfer review")
                if (pending["parcel_id"] != selected_parcel_id
                        or pending["destination_id"] != selected_destination_id
                        or pending.get("manager_override", False) != manager_override):
                    st.warning("Your selection has changed. Preview the transfer again before confirming.")
                    st.session_state.pop("pending_transfer", None)
                else:
                    review1, review2, review3 = st.columns(3)
                    review1.metric("Parcel", pending["parcel_code"])
                    review2.metric("Size", pending["size"])
                    review3.metric("Destination matching spaces", pending["available"])
                    st.write(f"**From:** {pending['source_name']}  →  **To:** {pending['destination_name']}")
                    st.warning("Confirming will update the parcel's current locker and add a record to ParcelMovement in Azure SQL.")
                    confirm_col, cancel_col = st.columns(2)
                    with confirm_col:
                        if st.button("Confirm and save transfer", type="primary", disabled=bool(validation_errors)):
                            try:
                                with st.spinner("Rechecking capacity and saving transfer..."):
                                    transfer_result = execute_parcel_transfer(
                                        pending["parcel_id"], pending["destination_id"],
                                        manager_override=pending.get("manager_override", False)
                                    )
                                st.session_state["last_transfer_result"] = transfer_result
                                st.session_state.pop("pending_transfer", None)
                                st.session_state.pop("manual_parcel_id", None)
                                st.session_state.pop("manual_destination_id", None)
                                load_lockers.clear()
                                load_parcels.clear()
                                load_recent_movements.clear()
                                load_transfer_recommendations.clear()
                                st.session_state.pop("network_optimiser_plan", None)
                                st.session_state.pop("network_optimiser_value", None)
                                st.session_state.pop("network_optimiser_generated_at", None)
                                st.rerun()
                            except Exception as error:
                                st.error(f"Transfer was not completed. No partial changes were saved: {error}")
                    with cancel_col:
                        if st.button("Cancel transfer"):
                            st.session_state.pop("pending_transfer", None)
                            st.rerun()

        st.subheader("Live parcel location map")
        st.caption("The same Esri street-map layer and utilisation-circle markers used in Network Overview. Click a locker to inspect Small, Medium and Large occupancy.")
        try:
            pm = make_locker_map(lockers)
            st_folium(pm, use_container_width=True, height=560, returned_objects=[], key="parcel_management_network_map_v3")
        except Exception as map_error:
            st.warning("The parcel-management map could not render. The table and transfer controls remain available.")
            with st.expander("Map technical details"):
                st.exception(map_error)

        st.subheader("Parcels currently at lockers")
        st.dataframe(
            [{"Parcel code": p["code"], "Size": p["size"], "Current locker": p["locker_name"],
              "Expected delivery": p["expected"], "Last status update": p["last_status"]}
             for p in parcels],
            use_container_width=True, hide_index=True
        )
    else:
        st.info("No parcels with status 'AtLocker' were returned from the database.")

    st.divider()
    st.subheader("2. Nationwide coordinated transfer optimiser")
    st.markdown(
        "The optimiser evaluates every feasible parcel-to-locker match across all active lockers, "
        "including cross-region moves. It balances Small, Medium and Large utilisation separately, "
        "accounts for destination capacity, regional importance and estimated transport cost, and "
        "selects grouped, coordinated movements. Each parcel can appear at most once; for each "
        "compartment size, a locker sends or receives in a plan, not both."
    )
    st.caption(
        "Recommendations are a simulation only. They do not change parcel records. "
        "A 24-hour cooldown excludes recently transferred parcels."
    )

    value_col, run_col = st.columns([2, 1])
    with value_col:
        eur_per_point = st.number_input(
            "Modelled value of a 1 percentage-point reduction in size-specific network utilisation spread (€)",
            min_value=0.0, max_value=10000.0, value=10.0, step=1.0,
            help="An editable economic modelling assumption, not actual Latvijas Pasts savings. "
                 "The optimiser multiplies this value by the reduction in weighted standard deviation "
                 "for the relevant compartment size."
        )
    with run_col:
        st.write("")
        run_optimizer = st.button(
            "Run nationwide optimiser", type="primary",
            disabled=bool(validation_errors), use_container_width=True
        )

    if run_optimizer:
        try:
            with st.spinner("Evaluating all feasible parcel and locker combinations..."):
                # Force a fresh database snapshot when the user explicitly runs the optimiser.
                load_lockers.clear()
                load_transfer_recommendations.clear()
                current_lockers = load_lockers()
                all_opportunities = load_transfer_recommendations()
                plan = optimise_network_transfers(
                    current_lockers, all_opportunities, eur_per_balance_point=eur_per_point
                )
            st.session_state["network_optimiser_plan"] = plan
            st.session_state["network_optimiser_value"] = eur_per_point
            st.session_state["network_optimiser_generated_at"] = __import__("datetime").datetime.now()
        except Exception as error:
            st.error("The nationwide optimiser could not be run. Check the transfer-candidate view and database schema.")
            with st.expander("Technical details"):
                st.exception(error)

    plan = st.session_state.get("network_optimiser_plan")
    if plan:
        if st.session_state.get("network_optimiser_value") != eur_per_point:
            st.info("The value assumption has changed. Run the optimiser again to refresh the plan.")
        else:
            generated_at = st.session_state.get("network_optimiser_generated_at")
            if generated_at:
                st.caption(f"Plan calculated: {generated_at:%Y-%m-%d %H:%M:%S}")
            st.markdown("#### Proposed plan summary")
            summary1, summary2, summary3, summary4 = st.columns(4)
            summary1.metric("Recommended transfers", len(plan["recommendations"]))
            summary2.metric("Estimated transport cost", f"€{plan['total_cost']:.2f}")
            summary3.metric("Modelled balance benefit", f"€{plan['total_benefit']:.2f}")
            summary4.metric("Modelled net benefit", f"€{plan['total_net_benefit']:.2f}")

            spread_rows = []
            for size in ("Small", "Medium", "Large"):
                before = plan["initial_spread"][size]
                after = plan["final_spread"][size]
                spread_rows.append({
                    "Compartment size": size,
                    "Weighted utilisation spread before (pp)": round(before, 3),
                    "Weighted utilisation spread after (pp)": round(after, 3),
                    "Improvement (pp)": round(before - after, 3),
                })
            st.markdown("#### Size-specific network balance")
            st.dataframe(spread_rows, use_container_width=True, hide_index=True)

            st.markdown("#### Before-and-after utilisation across all lockers")
            st.caption("The table shows projected utilisation for every locker and compartment size. Negative parcel changes mean parcels leave; positive changes mean parcels arrive.")
            st.dataframe(plan["locker_changes"], use_container_width=True, hide_index=True)
            st.markdown("#### Recommended movement network")
            arrows_on = st.checkbox("Show final recommended movement arrows", value=True, key="plan_map_arrows")
            plan_map = make_transfer_plan_map(current_lockers if "current_lockers" in locals() else load_lockers(), plan, arrows_on)
            st_folium(plan_map, use_container_width=True, height=600, returned_objects=[], key="recommended_plan_map")
            if plan.get("movements"):
                st.dataframe([{
                    "From": x["SourceLockerName"], "To": x["DestinationLockerName"],
                    "Size": x["ParcelSize"], "Parcels": x["ParcelCount"],
                    "Parcel IDs": ", ".join(x["ParcelCodes"]),
                    "Road km": round(x["EstimatedRoadDistanceKm"], 2),
                    "Group cost (€)": round(x["TotalCostEUR"], 2),
                } for x in plan["movements"]], use_container_width=True, hide_index=True)

            if plan["recommendations"]:
                st.markdown("#### Recommended coordinated transfers")
                st.dataframe(
                    [{
                        "Parcel": r["ParcelCode"],
                        "Size": r["ParcelSize"],
                        "From": f"{r['SourceLockerName']} ({r.get('SourceRegionName', '')})",
                        "To": f"{r['DestinationLockerName']} ({r.get('DestinationRegionName', '')})",
                        "Distance (road km)": round(float(r.get("EstimatedRoadDistanceKm") or 0), 2),
                        "Transport cost (€)": round(r["EstimatedTransportCostEUR"], 2),
                        "Balance improvement (pp)": round(r["BalanceImprovementPoints"], 4),
                        "Modelled net benefit (€)": round(r["EstimatedNetBenefitEUR"], 2),
                    } for r in plan["recommendations"]],
                    use_container_width=True, hide_index=True
                )
                st.warning(
                    "These are proposed transfers only. Cross-region recommendations may change the parcel's "
                    "physical pickup location; customer notification, service commitments and operational "
                    "approval are not yet modelled. No parcel has been moved."
                )
                st.markdown("#### Commit movements to transport planning")
                st.caption("This creates persistent transport jobs in Azure SQL. It does not change Parcel.CurrentLockerID or write ParcelMovement records.")
                if st.button("Commit approved movements for route optimisation", type="primary", key="commit_transport_jobs"):
                    try:
                        plan_id = commit_transport_plan(
                            plan["recommendations"],
                            st.session_state.get("network_optimiser_generated_at"),
                        )
                        st.session_state["last_committed_transport_plan"] = plan_id
                        st.success(f"Transport jobs committed. Plan reference: {plan_id}. Open Route Optimisation to build vehicle routes.")
                    except Exception as error:
                        st.error("The transport plan could not be committed. No parcel records were moved.")
                        with st.expander("Technical details"):
                            st.exception(error)
            else:
                st.info(
                    "No transfer combination has a positive modelled net benefit under the current "
                    "value-per-balance-point assumption and transport-cost estimates."
                )

            opportunities = plan["opportunities"]
            if opportunities:
                st.markdown("#### All feasible opportunities and rejected alternatives")
                st.caption(
                    f"Showing all {len(opportunities):,} parcel-to-locker opportunities returned by the database view, "
                    "with the optimiser's decision or rejection reason."
                )
                with st.expander("View all opportunities", expanded=False):
                    st.dataframe(
                        [{
                            "Parcel": r["ParcelCode"],
                            "Size": r["ParcelSize"],
                            "From": r["SourceLockerName"],
                            "To": r["DestinationLockerName"],
                            "Source region": r.get("SourceRegionName", ""),
                            "Destination region": r.get("DestinationRegionName", ""),
                            "Distance (road km)": round(float(r.get("EstimatedRoadDistanceKm") or 0), 2),
                            "Cost (€)": round(float(r.get("EstimatedTransportCostEUR") or 0), 2),
                            "Decision": r.get("Decision", "Not selected"),
                            "Reason": r.get("DecisionReason", ""),
                        } for r in opportunities],
                        use_container_width=True, hide_index=True
                    )

            st.caption(
                "Economic interpretation: modelled benefit = reduction in weighted size-specific utilisation "
                "spread × the editable EUR value per percentage point. Net benefit = modelled benefit − "
                "estimated one-way transfer cost. These are prototype assumptions, not measured savings."
            )
    else:
        st.info("Run the nationwide optimiser to calculate a coordinated transfer plan from the current database snapshot.")

    st.divider()
    st.subheader("3. Recent parcel movements")
    try:
        movements = load_recent_movements()
        if movements:
            st.dataframe(
                [{"Date": m["EventAt"], "Event": m["EventType"], "Parcel": m["ParcelCode"],
                  "From": m["FromLocker"] or "-", "To": m["ToLocker"] or "-",
                  "Estimated road km": m["EstimatedRoadDistanceKm"],
                  "Estimated cost (€)": m["EstimatedTransportCostEUR"]}
                 for m in movements],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No parcel movement records were found.")
    except Exception as error:
        st.warning("Movement history could not be loaded. If the database is an older schema version, some optional distance/cost columns may be missing.")
        with st.expander("Technical details"):
            st.exception(error)

    st.caption("All parcel records are synthetic demonstration data. Confirmed transfers write to the synthetic Azure SQL database and should not be interpreted as real Latvijas Pasts operations.")

elif page == "Route Optimisation":
    st.subheader("Multi-vehicle route optimisation")
    st.markdown("The planner builds multi-stop pickup-and-delivery tours. Trucks can collect parcels, carry them across several stops, drop off only the parcels assigned to that locker, and collect new parcels before continuing.")
    st.warning("Travel times and road distances are estimates based on straight-line distance × road factor and an assumed average speed. No live traffic or road-routing API is connected. A route plan is a simulation, not a dispatch instruction.")
    try:
        committed_jobs = load_committed_transport_jobs()
        active_plans = load_active_transport_plans()
    except Exception as error:
        committed_jobs, active_plans = [], []
        st.error("Could not load committed transport jobs or plans.")
        with st.expander("Technical details"): st.exception(error)

    st.markdown("#### Manage committed transport plans")
    if active_plans:
        labels={str(x["TransportPlanID"]):f"{str(x['TransportPlanID'])[:8]} — {x['CurrentParcelCount']} parcels — {x['CreatedAt']}" for x in active_plans}
        chosen=st.selectbox("Committed plan to cancel",list(labels),format_func=lambda x:labels[x],key="transport_plan_to_cancel")
        reason=st.text_input("Cancellation reason (optional)",key="transport_plan_cancel_reason")
        confirm=st.checkbox("I confirm that this plan should be cancelled and its parcels released for replanning.",key="confirm_transport_plan_cancel")
        if st.button("Cancel selected transport plan",disabled=not confirm,key="cancel_selected_transport_plan"):
            try:
                cancel_transport_plan(chosen,reason.strip() or None)
                st.session_state.pop("vehicle_route_result",None); st.session_state.pop("vehicle_route_job_signature",None)
                st.success("Plan cancelled. Parcel records and movement history were not changed."); st.rerun()
            except Exception as error: st.error(f"Cancellation failed: {error}")
    else: st.info("There are no active committed plans to cancel.")

    if active_plans:
        st.markdown("#### Execute approved parcel transfers")
        st.warning("This action permanently updates Parcel.CurrentLockerID and writes Transferred events to ParcelMovement in Azure SQL. All parcels in the selected plan are processed in one database transaction. If any validation fails, the entire plan is rolled back.")
        execute_labels = {str(x["TransportPlanID"]): f"{str(x['TransportPlanID'])[:8]} — {x['CurrentParcelCount']} parcels" for x in active_plans}
        execute_plan_id = st.selectbox("Plan to execute", list(execute_labels), format_func=lambda x: execute_labels[x], key="execute_transport_plan_choice")
        confirm_execute = st.checkbox("I confirm that these parcels have physically been transferred to their planned destination lockers.", key="confirm_execute_transport_plan")
        if st.button("Execute transfers and update Azure SQL", type="primary", disabled=not confirm_execute, key="execute_transport_plan_button"):
            try:
                with st.spinner("Validating parcel locations and compartment capacity, then updating Azure SQL..."):
                    moved_count = execute_transport_plan(execute_plan_id)
                load_lockers.clear(); load_parcels.clear(); load_transfer_recommendations.clear(); load_recent_movements.clear()
                st.session_state.pop("vehicle_route_result", None); st.session_state.pop("vehicle_route_job_signature", None)
                st.success(f"Successfully transferred {moved_count} parcels. Azure SQL has been updated.")
                st.rerun()
            except Exception as error:
                st.error(f"Transfer execution failed. No partial changes were saved: {error}")

    st.divider()
    if not committed_jobs:
        st.info("No committed transport jobs are waiting. First create and commit transfer recommendations in Parcel Management.")
    else:
        st.metric("Committed parcels awaiting routing",len(committed_jobs))
        st.dataframe([{"Parcel":x["ParcelCode"],"Size":x["ParcelSize"],"From":x["SourceLockerName"],"To":x["DestinationLockerName"],"Original lane km":round(float(x["EstimatedRoadDistanceKm"] or 0),2)} for x in committed_jobs],use_container_width=True,hide_index=True)
        lane_summary={}
        for job in committed_jobs:
            key=(job["SourceLockerCode"],job["SourceLockerName"],job["DestinationLockerCode"],job["DestinationLockerName"],job["ParcelSize"])
            lane_summary.setdefault(key,[]).append(job["ParcelCode"])
        st.markdown("#### Total committed movements by origin and destination")
        st.dataframe([{"From":f"{k[1]} ({k[0]})","To":f"{k[3]} ({k[2]})","Small":sum(1 for j in committed_jobs if j["SourceLockerCode"]==k[0] and j["DestinationLockerCode"]==k[2] and j["ParcelSize"]=="Small"),"Medium":sum(1 for j in committed_jobs if j["SourceLockerCode"]==k[0] and j["DestinationLockerCode"]==k[2] and j["ParcelSize"]=="Medium"),"Large":sum(1 for j in committed_jobs if j["SourceLockerCode"]==k[0] and j["DestinationLockerCode"]==k[2] and j["ParcelSize"]=="Large"),"Parcel count":len(v)} for k,v in lane_summary.items()],use_container_width=True,hide_index=True)
        # Routing diagnostics: expose the actual geographic structure of the committed jobs
        # before changing fleet logic. This helps distinguish disconnected regions from
        # a single weakly-connected graph joined by a few cross-region transfers.
        with st.expander("Routing diagnostics: regional distribution and connected groups", expanded=True):
            region_pairs = {}
            for job in committed_jobs:
                key = (job.get("SourceRegionName", "Unknown"), job.get("DestinationRegionName", "Unknown"))
                region_pairs[key] = region_pairs.get(key, 0) + 1
            st.markdown("**Parcel movements by source region → destination region**")
            st.dataframe([
                {"Source region": a, "Destination region": b, "Parcels": n}
                for (a, b), n in sorted(region_pairs.items(), key=lambda x: (-x[1], x[0]))
            ], use_container_width=True, hide_index=True)
            diagnostic_groups = _job_components(committed_jobs)
            st.caption(f"Current locker-connectivity algorithm identifies {len(diagnostic_groups)} weakly connected group(s). A cross-region transfer can connect otherwise distant areas into one group.")
            group_rows = []
            for gi, group in enumerate(diagnostic_groups, 1):
                regions = sorted({str(j.get("SourceRegionName", "Unknown")) for j in group} | {str(j.get("DestinationRegionName", "Unknown")) for j in group})
                lockers_in_group = sorted({str(j.get("SourceLockerCode", "")) for j in group} | {str(j.get("DestinationLockerCode", "")) for j in group})
                group_rows.append({"Current group": gi, "Parcels": len(group), "Regions involved": ", ".join(regions), "Lockers involved": len(lockers_in_group), "Locker codes": ", ".join(lockers_in_group)})
            st.dataframe(group_rows, use_container_width=True, hide_index=True)
            st.markdown("**Individual committed movements**")
            st.dataframe([{
                "Parcel": j["ParcelCode"], "Size": j["ParcelSize"],
                "From": f"{j.get('SourceLockerName','')} ({j.get('SourceLockerCode','')})",
                "Source region": j.get("SourceRegionName", "Unknown"),
                "To": f"{j.get('DestinationLockerName','')} ({j.get('DestinationLockerCode','')})",
                "Destination region": j.get("DestinationRegionName", "Unknown"),
                "Distance km": round(float(j.get("EstimatedRoadDistanceKm") or 0), 2),
            } for j in committed_jobs], use_container_width=True, hide_index=True)
        locker_options={}
        for x in committed_jobs:
            locker_options[int(x["SourceLockerID"])]=f"{x.get('SourceLockerCode','')} | {x['SourceLockerName']}"
            locker_options[int(x["DestinationLockerID"])]=f"{x.get('DestinationLockerCode','')} | {x['DestinationLockerName']}"
        c1,c2,c3=st.columns(3)
        with c1:
            auto_depot=st.checkbox("Automatically choose the central start/end depot",value=True,key="route_auto_depot")
            manual_depot=st.selectbox("Manual depot override",list(locker_options),format_func=lambda x:locker_options[x],key="route_depot",disabled=auto_depot)
            depot_id=choose_central_depot(committed_jobs) if auto_depot else manual_depot
            st.caption(f"Selected depot: {locker_options.get(depot_id, depot_id)}")
            fleet_mode=st.radio("Fleet size selection", ["Automatic (optimiser chooses)", "Manual (set exact truck count)"], horizontal=True, key="route_fleet_mode")
            max_vehicles=st.number_input("Maximum available trucks" if fleet_mode.startswith("Automatic") else "Available fleet limit",1,20,8,1,key="route_max_vehicles")
            manual_trucks = None
            if fleet_mode.startswith("Manual"):
                manual_trucks=st.number_input("Trucks to dispatch",1,int(max_vehicles),min(5,int(max_vehicles)),1,key="route_manual_trucks")
        with c2:
            capacity=st.number_input("Capacity per truck (parcels)",1,5000,100,5,key="route_capacity")
            eur_per_km=st.number_input("Operating cost per km (€)",0.0,100.0,0.65,0.05,key="route_eur_km")
        with c3:
            fixed_cost=st.number_input("Fixed cost per truck dispatched (€)",0.0,1000.0,25.0,1.0,key="route_fixed_cost")
            road_factor=st.number_input("Road detour factor",1.0,2.5,1.30,0.05,key="route_road_factor")
        c4,c5,c6=st.columns(3)
        with c4: speed=st.number_input("Assumed average speed (km/h)",10.0,100.0,45.0,5.0,key="route_speed")
        with c5: handling=st.number_input("Handling time per stop (minutes)",0,60,8,1,key="route_handling")
        with c6: time_value=st.number_input("Value of 1 truck-hour (€)",0.0,100.0,12.0,1.0,key="route_time_value",help="Used to balance lower operating cost against faster completion. This is an editable planning assumption.")

        if st.button("Automatically optimise fleet and routes",type="primary",key="run_vehicle_route_optimiser"):
            try:
                with st.spinner("Testing different fleet sizes and sequencing pickup/drop-off stops..."):
                    result=choose_fleet_plan(committed_jobs,depot_id,int(max_vehicles),int(capacity),float(eur_per_km),float(fixed_cost),float(road_factor),float(speed),int(handling),float(time_value),lockers=lockers,exact_vehicles=int(manual_trucks) if manual_trucks is not None else None)
                st.session_state["vehicle_route_result"]=result
                st.session_state["vehicle_route_job_signature"]=tuple(sorted(int(x["ParcelID"]) for x in committed_jobs))
            except Exception as error:
                st.error("Route optimisation could not be completed.")
                with st.expander("Technical details"): st.exception(error)

        result=st.session_state.get("vehicle_route_result")
        signature=tuple(sorted(int(x["ParcelID"]) for x in committed_jobs))
        if result and st.session_state.get("vehicle_route_job_signature")==signature:
            st.markdown("#### Recommended fleet plan")
            a,b,c,d,e=st.columns(5)
            a.metric("Trucks used",result["vehicles_used"])
            b.metric("Total route distance",f"{result['total_km']:.1f} km")
            c.metric("Transport cost",f"€{result['total_cost']:.2f}")
            d.metric("Time until last truck finishes",f"{result['total_hours']:.1f} h")
            e.metric("Parcels assigned",result["parcel_count"])
            st.caption(f"The jobs form {result['component_count']} disconnected transfer groups, each assigned at least one locally based truck. Extra trucks are allocated to the heaviest groups. Fleet selection balances estimated transport cost and completion time; average source-locker utilisation is {result['source_congestion_percent']:.1f}%. These are illustrative estimates, not live dispatch data.")
            cheap, fast = st.columns(2)
            cheap.metric("Cheapest tested fleet", f"{result['cheapest_option']['vehicles']} truck(s) | €{result['cheapest_option']['cost']:.2f}", help=f"Estimated completion: {result['cheapest_option']['hours']:.1f} hours")
            fast.metric("Fastest tested fleet", f"{result['fastest_option']['vehicles']} truck(s) | {result['fastest_option']['hours']:.1f} h", help=f"Estimated cost: €{result['fastest_option']['cost']:.2f}")
            st.markdown("#### Fleet size comparison")
            st.dataframe(result["fleet_comparison"],use_container_width=True,hide_index=True)
            st.markdown("#### Truck routes and parcel movements")
            st.caption("Each colour represents one truck. Directional arrows follow the truck’s sequential route. Hover over an arrow or road leg for Small/Medium/Large parcel counts; click for the parcel IDs grouped by size.")
            # Reuse the working Network Overview base map and occupancy-circle markers.
            route_map=make_locker_map(lockers)
            palette={"Small":"#238b75","Medium":"#d18b24","Large":"#c44757"}
            locker_lookup={int(x["id"]):x for x in lockers}
            # Each truck is shown as one consistently coloured sequential route.
            # Each road leg gets a visible, direction-aware arrow marker. Hover the
            # arrow for Small/Medium/Large counts; click it for the parcel manifest.
            def route_bearing_degrees(lat1, lon1, lat2, lon2):
                p1, p2 = math.radians(lat1), math.radians(lat2)
                dl = math.radians(lon2 - lon1)
                y = math.sin(dl) * math.cos(p2)
                x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
                return (math.degrees(math.atan2(y, x)) + 360) % 360
            truck_palette = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#4d7c0f", "#7c3aed", "#0f766e", "#b45309"]
            for route_index, route in enumerate(result["routes"]):
                route["_map_colour"] = truck_palette[route_index % len(truck_palette)]
            legend_items = "".join(
                f'<span style="display:inline-flex;align-items:center;margin:2px 9px 2px 0;'
                f'font:12px Arial;color:#222"><span style="display:inline-block;width:11px;height:11px;'
                f'background:{route["_map_colour"]};border:1px solid white;border-radius:50%;'
                f'margin-right:5px"></span>{html.escape(str(route["Vehicle"]))}</span>'
                for route in result["routes"]
            )
            legend_html = (
                '<div style="background:rgba(255,255,255,.94);padding:7px 10px;border-radius:5px;'
                'box-shadow:0 1px 5px #777;max-width:260px">'
                '<b style="font:12px Arial;color:#222">Truck routes</b><br>'
                + legend_items + '</div>'
            )
            folium.Marker(
                [lockers[0]["lat"], lockers[0]["lon"]],
                icon=folium.DivIcon(
                    html=legend_html,
                    icon_size=(260, 90),
                    icon_anchor=(0, 0),
                    class_name="truck-route-legend"
                ),
                interactive=False,
            ).add_to(route_map)
            for route in result["routes"]:
                stops=route["Stops"]
                for stop in stops:
                    details="<br>".join(f"{html.escape(x['Code'])} ({html.escape(x['Size'])})" for x in stop["ParcelDetails"])
                    onboard="<br>".join(f"{html.escape(x['Code'])} ({html.escape(x['Size'])})" for x in stop["OnboardAfterStop"]) or "None"
                    popup=(f"<b>{html.escape(route['Vehicle'])}: {html.escape(stop['Action'])}</b><br>"
                           f"Locker: {html.escape(stop['LockerCode'])} | {html.escape(stop['Locker'])}<br>"
                           f"Parcels handled:<br>{details}<hr>On truck after stop:<br>{onboard}")
                    folium.CircleMarker([stop["Latitude"],stop["Longitude"]],radius=8,color="#26364a",fill=True,fill_opacity=.95,
                        tooltip=f"{route['Vehicle']} | {stop['Action']} | {stop['LockerCode']}",popup=folium.Popup(popup,max_width=360)).add_to(route_map)
                # Draw each consecutive truck leg in that truck's colour. Hover shows
                # size counts; clicking shows exact parcel IDs grouped by size.
                truck_colour = route.get("_map_colour", "#2563eb")
                for i in range(len(stops)-1):
                    current, next_stop = stops[i], stops[i+1]
                    carried = current["OnboardAfterStop"]
                    grouped = {"Small": [], "Medium": [], "Large": []}
                    for parcel in carried:
                        grouped.setdefault(parcel["Size"], []).append(parcel["Code"])
                    counts = {size: len(codes) for size, codes in grouped.items()}
                    count_text = " | ".join(f"{size}: {counts[size]}" for size in ("Small", "Medium", "Large"))
                    cargo_html = "".join(
                        f"<b>{size} ({len(codes)})</b><br>" + ("<br>".join(html.escape(code) for code in codes) if codes else "None") + "<br><br>"
                        for size, codes in grouped.items()
                    )
                    leg_km = haversine_km(current["Latitude"], current["Longitude"], next_stop["Latitude"], next_stop["Longitude"]) * float(road_factor)
                    leg_minutes = leg_km / max(float(speed), 1) * 60
                    tip = (f"{route['Vehicle']}: {current['Locker']} ({current['LockerCode']}) → "
                           f"{next_stop['Locker']} ({next_stop['LockerCode']})<br>{len(carried)} parcels | {count_text}")
                    popup = (f"<b>{html.escape(route['Vehicle'])}: parcel movement</b><br>"
                             f"From: {html.escape(current['Locker'])} ({html.escape(current['LockerCode'])})<br>"
                             f"To: {html.escape(next_stop['Locker'])} ({html.escape(next_stop['LockerCode'])})<br>"
                             f"Estimated leg: {leg_km:.1f} km / {leg_minutes:.0f} min<hr>"
                             f"<b>Parcels onboard on this leg</b><br>{cargo_html}")
                    leg_line = folium.PolyLine(
                        [[current["Latitude"], current["Longitude"]], [next_stop["Latitude"], next_stop["Longitude"]]],
                        color=truck_colour, weight=6, opacity=.9,
                        tooltip=folium.Tooltip(tip, sticky=True, direction="top"),
                        popup=folium.Popup(popup, max_width=420, max_height=420),
                    ).add_to(route_map)
                    # Draw a high-contrast SVG arrow as a separate interactive marker.
                    # SVG is more reliable than a rotated text glyph across browsers.
                    mid_lat = current["Latitude"] * 0.45 + next_stop["Latitude"] * 0.55
                    mid_lon = current["Longitude"] * 0.45 + next_stop["Longitude"] * 0.55
                    bearing = route_bearing_degrees(
                        current["Latitude"], current["Longitude"],
                        next_stop["Latitude"], next_stop["Longitude"]
                    )
                    rotation = bearing - 90  # SVG points east by default; rotate to route bearing.
                    arrow_html = f"""
                    <div style="width:42px;height:42px;display:flex;align-items:center;justify-content:center;
                                cursor:pointer;pointer-events:auto;filter:drop-shadow(0 1px 2px #fff);">
                      <svg xmlns="http://www.w3.org/2000/svg" width="38" height="38" viewBox="0 0 36 36"
                           style="overflow:visible;transform:rotate({rotation:.1f}deg);">
                        <path d="M3 11 L23 11 L23 4 L34 18 L23 32 L23 25 L3 25 Z"
                              fill="{truck_colour}" stroke="#ffffff" stroke-width="2.5"
                              stroke-linejoin="round"/>
                      </svg>
                    </div>
                    """
                    folium.Marker(
                        [mid_lat, mid_lon],
                        icon=folium.DivIcon(
                            html=arrow_html,
                            icon_size=(42, 42),
                            icon_anchor=(21, 21),
                            class_name="truck-direction-arrow"
                        ),
                        tooltip=folium.Tooltip(tip, sticky=True, direction="top", opacity=0.98),
                        popup=folium.Popup(popup, max_width=460, max_height=480),
                        z_index_offset=3000,
                    ).add_to(route_map)
            # Render as standalone Leaflet HTML to preserve reliable pan/zoom and
            # ensure the custom direction arrows, hover tooltips and click popups load.
            route_map.options["dragging"] = True
            route_map_html = route_map.get_root().render()
            components.html(route_map_html, height=700, scrolling=False)
            for route in result["routes"]:
                with st.expander(f"{route['Vehicle']} | {route['DistanceKm']:.1f} km | {route['DurationHours']:.1f} h | peak load {route['PeakLoadParcels']} parcels",expanded=True):
                    st.dataframe([{"Stop":x["Stop"],"Action":x["Action"],"Locker code":x["LockerCode"],"Locker":x["Locker"],"Estimated elapsed time (min)":x["ElapsedMinutes"],"Parcels handled":", ".join(f"{p['Code']} ({p['Size']})" for p in x["ParcelDetails"]),"On truck after stop":", ".join(f"{p['Code']} ({p['Size']})" for p in x["OnboardAfterStop"])} for x in route["Stops"]],use_container_width=True,hide_index=True)
            st.success("Route plan calculated. No parcel has been moved and no truck has been dispatched. Execute transfers separately after operational approval.")


elif page == "Capacity Scenarios":
    st.subheader("Capacity Scenarios")
    st.info(
        "This section is planned for the capacity milestone: "
        "simulate higher demand and identify potential locker bottlenecks "
        "without changing the underlying database."
    )
