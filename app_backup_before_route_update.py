import html
import math
import os
from pathlib import Path

import folium
import pyodbc
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium


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
    """Coordinated grouped redistribution, independently by compartment size.

    Uses a weighted network-balance score with higher importance for Riga and
    nearby regions. Transfers are selected as source/destination/size groups;
    single-parcel groups are allowed only for critically congested sources.
    This remains a planning heuristic, not a vehicle-routing or measured-savings model.
    """
    sizes = ("Small", "Medium", "Large")
    locker_by_id = {int(x["id"]): x for x in lockers}
    states = {size: _size_balance_state(lockers, size) for size in sizes}
    initial_spread = {size: states[size]["spread"] for size in sizes}
    initial_occupancy = {size: dict(states[size]["occupied"]) for size in sizes}

    def region_weight(locker):
        region = str(locker.get("region", "")).lower()
        if "riga" == region or region.startswith("riga "):
            return 1.35
        if "pier" in region:
            return 1.15
        return 1.0

    def target_for(locker):
        region = str(locker.get("region", "")).lower()
        if "riga" == region or region.startswith("riga "):
            return 0.85
        if "pier" in region:
            return 0.70
        return 0.60

    def network_score(size, occupancy):
        state = states[size]
        score = 0.0
        for lid, cap in state["capacities"].items():
            if cap <= 0:
                continue
            locker = locker_by_id[lid]
            util = occupancy[lid] / cap
            target = target_for(locker)
            # Congestion above target is penalised more sharply; critical crowding
            # receives an additional convex penalty. Underutilisation is also visible
            # but less costly than overfilling an important locker.
            over = max(0.0, util - target)
            under = max(0.0, target - util)
            critical = max(0.0, util - 0.92)
            score += region_weight(locker) * cap * (
                2.2 * over * over + 0.30 * under * under + 8.0 * critical * critical
            )
        return score

    selected = []
    used_parcels = set()
    # Per locker and size, movement direction is fixed for this plan.
    directions = {}  # (locker_id, size) -> 'send' or 'receive'
    blocked_recent = {int(c["ParcelID"]) for c in candidates
                      if bool(c.get("RecentTransferWithin24Hours", False))}
    candidate_rows = [dict(c) for c in candidates if int(c["ParcelID"]) not in blocked_recent]

    # Re-evaluate the best grouped lane after every accepted group, so all selected
    # movements are coordinated against the projected network state.
    while True:
        best_group = None
        best_net = 0.0
        grouped = {}
        for c in candidate_rows:
            pid = int(c["ParcelID"])
            size = str(c["ParcelSize"])
            src, dst = int(c["SourceLockerID"]), int(c["DestinationLockerID"])
            if pid in used_parcels or size not in states or src == dst:
                continue
            if (src, size) in directions and directions[(src, size)] != "send":
                continue
            if (dst, size) in directions and directions[(dst, size)] != "receive":
                continue
            if states[size]["occupied"].get(dst, 0) >= states[size]["capacities"].get(dst, 0):
                continue
            grouped.setdefault((src, dst, size), []).append(c)

        for (src, dst, size), rows in grouped.items():
            rows = sorted(rows, key=lambda x: float(x.get("EstimatedTransportCostEUR") or 0))
            source = locker_by_id[src]
            cap = states[size]["capacities"].get(src, 0)
            occ = states[size]["occupied"].get(src, 0)
            if cap <= 0 or occ <= 0:
                continue
            source_util = occ / cap
            # Avoid a one-parcel transfer unless the source is critically congested.
            min_group = 1 if source_util >= 0.95 else 2
            max_n = min(len(rows), occ, states[size]["capacities"].get(dst, 0)
                        - states[size]["occupied"].get(dst, 0))
            if max_n < min_group:
                continue
            before = network_score(size, states[size]["occupied"])
            trial_occ = dict(states[size]["occupied"])
            lane_cost = 0.0
            lane_rows = []
            # Try increasing group sizes; select the most cost-effective positive lane.
            for n, row in enumerate(rows[:max_n], start=1):
                lane_rows.append(row)
                lane_cost += float(row.get("EstimatedTransportCostEUR") or 0.0)
                if n < min_group:
                    continue
                trial_occ[src] -= 1
                trial_occ[dst] += 1
                improvement = before - network_score(size, trial_occ)
                benefit = improvement * float(eur_per_balance_point)
                net = benefit - lane_cost
                if net > best_net:
                    best_net = net
                    best_group = (src, dst, size, list(lane_rows), improvement, benefit, lane_cost, net)

        if best_group is None:
            break
        src, dst, size, rows, improvement, benefit, cost, net = best_group
        for row in rows:
            pid = int(row["ParcelID"])
            used_parcels.add(pid)
            selected.append({
                **row,
                "BalanceImprovementPoints": improvement / len(rows),
                "ModelledBalanceBenefitEUR": benefit / len(rows),
                "EstimatedTransportCostEUR": float(row.get("EstimatedTransportCostEUR") or 0.0),
                "EstimatedNetBenefitEUR": net / len(rows),
                "GroupSize": len(rows),
                "Decision": "Recommended",
                "DecisionReason": f"Selected in coordinated group of {len(rows)} parcels",
            })
        directions[(src, size)] = "send"
        directions[(dst, size)] = "receive"
        states[size]["occupied"][src] -= len(rows)
        states[size]["occupied"][dst] += len(rows)
        states[size]["sum_sq"] = sum(
            ((states[size]["occupied"][lid] - states[size]["mean"] * cap) ** 2) / cap
            for lid, cap in states[size]["capacities"].items() if cap > 0
        )
        states[size]["spread"] = (states[size]["sum_sq"] / states[size]["total_capacity"]) ** 0.5 * 100.0

    selected_ids = {int(x["ParcelID"]) for x in selected}
    selected_lane = {(int(x["SourceLockerID"]), int(x["DestinationLockerID"]), str(x["ParcelSize"]))
                     for x in selected}
    decisions = []
    for c in candidates:
        row = dict(c)
        pid = int(c["ParcelID"])
        src, dst, size = int(c["SourceLockerID"]), int(c["DestinationLockerID"]), str(c["ParcelSize"])
        if pid in selected_ids:
            row.update(Decision="Recommended", DecisionReason="Included in coordinated grouped plan")
        elif bool(c.get("RecentTransferWithin24Hours", False)):
            row.update(Decision="Blocked", DecisionReason="Parcel transferred within the last 24 hours")
        elif (src, size) in directions and directions[(src, size)] != "send":
            row.update(Decision="Blocked", DecisionReason="Locker is receiving this compartment size in the plan")
        elif (dst, size) in directions and directions[(dst, size)] != "receive":
            row.update(Decision="Blocked", DecisionReason="Locker is sending this compartment size in the plan")
        elif (src, dst, size) not in selected_lane:
            row.update(Decision="Not selected", DecisionReason="Not part of the best positive-value grouped movement")
        decisions.append(row)

    final_occupancy = {size: dict(states[size]["occupied"]) for size in sizes}
    final_spread = {size: states[size]["spread"] for size in sizes}
    locker_changes = []
    for locker in lockers:
        lid = int(locker["id"])
        item = {"Locker": locker["name"], "Region": locker["region"]}
        for size in sizes:
            key = size.lower()
            before = initial_occupancy[size][lid]
            after = final_occupancy[size][lid]
            cap = states[size]["capacities"][lid]
            item[f"{size} before %"] = round(100 * before / cap, 1) if cap else 0
            item[f"{size} after %"] = round(100 * after / cap, 1) if cap else 0
            item[f"{size} change"] = after - before
        locker_changes.append(item)

    # Consolidated lanes drive the map and group-level operational view.
    lanes = {}
    for x in selected:
        key = (int(x["SourceLockerID"]), int(x["DestinationLockerID"]), str(x["ParcelSize"]))
        lanes.setdefault(key, []).append(x)
    movements = []
    for (src, dst, size), parcels in lanes.items():
        first = parcels[0]
        movements.append({
            "SourceLockerID": src, "DestinationLockerID": dst, "ParcelSize": size,
            "SourceLockerName": first["SourceLockerName"],
            "DestinationLockerName": first["DestinationLockerName"],
            "ParcelCount": len(parcels), "ParcelCodes": [x["ParcelCode"] for x in parcels],
            "TotalCostEUR": sum(float(x["EstimatedTransportCostEUR"] or 0) for x in parcels),
            "EstimatedRoadDistanceKm": float(first.get("EstimatedRoadDistanceKm") or 0),
        })

    return {
        "recommendations": selected, "opportunities": decisions,
        "initial_spread": initial_spread, "final_spread": final_spread,
        "initial_occupancy": initial_occupancy, "final_occupancy": final_occupancy,
        "locker_changes": locker_changes, "movements": movements,
        "total_cost": sum(float(x["EstimatedTransportCostEUR"] or 0) for x in selected),
        "total_benefit": sum(float(x["ModelledBalanceBenefitEUR"] or 0) for x in selected),
        "total_net_benefit": sum(float(x["EstimatedNetBenefitEUR"] or 0) for x in selected),
        "balance_value_eur": float(eur_per_balance_point),
    }


def make_transfer_plan_map(lockers, plan, show_arrows=True):
    """Map final recommended lanes only; lines are schematic straight-line links."""
    m = folium.Map(location=[56.95, 24.6], zoom_start=7, tiles="CartoDB positron", control_scale=True)
    by_id = {int(x["id"]): x for x in lockers}
    for locker in lockers:
        lid = int(locker["id"])
        parts = []
        for size in ("Small", "Medium", "Large"):
            cap = int(locker[f"{size.lower()}_capacity"])
            before = int(plan["initial_occupancy"][size][lid])
            after = int(plan["final_occupancy"][size][lid])
            b = 100 * before / cap if cap else 0
            a = 100 * after / cap if cap else 0
            parts.append(f"{size}: {b:.1f}% → {a:.1f}%")
        popup = f"<b>{html.escape(locker['name'])}</b><br>{html.escape(locker['region'])}<br>" + "<br>".join(parts)
        folium.CircleMarker([locker["lat"], locker["lon"]], radius=7,
            color="#253858", weight=2, fill=True, fill_opacity=.9,
            tooltip=html.escape(locker["name"]), popup=folium.Popup(popup, max_width=300)).add_to(m)
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


def make_locker_map(lockers):
    map_object = folium.Map(
        location=[56.95, 24.6],
        zoom_start=7,
        tiles=None,
        control_scale=True,
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
        utilisation = min(100, max(0, locker["utilisation"]))
        colour = occupancy_colour(utilisation)

        name = html.escape(locker["name"])
        region = html.escape(locker["region"])
        code = html.escape(locker["code"])

        small_ring = make_occupancy_ring(
            locker["small_occupied"], locker["small_capacity"]
        )
        medium_ring = make_occupancy_ring(
            locker["medium_occupied"], locker["medium_capacity"]
        )
        large_ring = make_occupancy_ring(
            locker["large_occupied"], locker["large_capacity"]
        )

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
                    {locker['occupied']} / {locker['capacity']}
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
                        {locker['small_occupied']} / {locker['small_capacity']}
                    </span>
                </div>
                <div style="flex:1;">
                    {medium_ring}
                    <b>MEDIUM</b><br>
                    <span style="font-size:13px;color:#666;">
                        {locker['medium_occupied']} / {locker['medium_capacity']}
                    </span>
                </div>
                <div style="flex:1;">
                    {large_ring}
                    <b>LARGE</b><br>
                    <span style="font-size:13px;color:#666;">
                        {locker['large_occupied']} / {locker['large_capacity']}
                    </span>
                </div>
            </div>
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
        locker_map = make_locker_map(filtered_lockers)
        st_folium(
            locker_map,
            use_container_width=True,
            height=650,
            returned_objects=[],
            key="locker_network_map",
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
    st.subheader("Route Optimisation")
    st.info(
        "This section is planned for the route milestone: "
        "select delivery locations and compare one-truck and two-truck "
        "route distances and estimated costs."
    )

elif page == "Capacity Scenarios":
    st.subheader("Capacity Scenarios")
    st.info(
        "This section is planned for the capacity milestone: "
        "simulate higher demand and identify potential locker bottlenecks "
        "without changing the underlying database."
    )
