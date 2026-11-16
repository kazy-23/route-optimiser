
import math
import os
from pathlib import Path

import folium
import pyodbc
from dotenv import load_dotenv
from ortools.constraint_solver import pywrapcp, routing_enums_pb2


# ============================================================
# 1. LOAD DATABASE CREDENTIALS
# ============================================================

project_folder = Path(__file__).resolve().parents[1]
load_dotenv(project_folder / ".env")

server = os.getenv("DB_SERVER")
database = os.getenv("DB_NAME")
username = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")

if not all([server, database, username, password]):
    raise ValueError(
        "Database credentials are missing. Check your .env file."
    )


def quote_odbc(value):
    """Safely format values containing special characters for ODBC."""
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


# ============================================================
# 2. RETRIEVE LOCKER AND OCCUPANCY DATA FROM AZURE SQL
# ============================================================

sql_query = """
WITH LockerCapacity AS (
    SELECT
        LockerID,
        SUM(TotalCompartments) AS TotalCapacity,

        SUM(
            CASE
                WHEN CompartmentSize = 'Small'
                THEN TotalCompartments
                ELSE 0
            END
        ) AS SmallCapacity,

        SUM(
            CASE
                WHEN CompartmentSize = 'Medium'
                THEN TotalCompartments
                ELSE 0
            END
        ) AS MediumCapacity,

        SUM(
            CASE
                WHEN CompartmentSize = 'Large'
                THEN TotalCompartments
                ELSE 0
            END
        ) AS LargeCapacity

    FROM dbo.LockerCompartment
    GROUP BY LockerID
),

ParcelOccupancy AS (
    SELECT
        CurrentLockerID AS LockerID,

        COUNT(*) AS Occupied,

        SUM(
            CASE
                WHEN ParcelSize = 'Small'
                THEN 1
                ELSE 0
            END
        ) AS SmallOccupied,

        SUM(
            CASE
                WHEN ParcelSize = 'Medium'
                THEN 1
                ELSE 0
            END
        ) AS MediumOccupied,

        SUM(
            CASE
                WHEN ParcelSize = 'Large'
                THEN 1
                ELSE 0
            END
        ) AS LargeOccupied

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


print("Connecting to Azure SQL...")

with pyodbc.connect(connection_string) as connection:

    cursor = connection.cursor()
    cursor.execute(sql_query)

    rows = cursor.fetchall()

    locations = []

    for row in rows:

        locker = {
            "id": row.LockerID,
            "code": row.LockerCode,
            "name": row.LockerName,
            "region": row.RegionName,

            "lat": float(row.Latitude),
            "lon": float(row.Longitude),

            "occupied": int(row.Occupied),
            "capacity": int(row.TotalCapacity),

            "small_occupied": int(row.SmallOccupied),
            "small_capacity": int(row.SmallCapacity),

            "medium_occupied": int(row.MediumOccupied),
            "medium_capacity": int(row.MediumCapacity),

            "large_occupied": int(row.LargeOccupied),
            "large_capacity": int(row.LargeCapacity),

            "utilisation": (
                float(row.UtilisationPercentage)
                if row.UtilisationPercentage is not None
                else 0
            )
        }

        locations.append(locker)


if not locations:
    raise ValueError("No active lockers were returned from the database.")

print(f"Successfully retrieved {len(locations)} lockers.")


# ============================================================
# 2.1. VALIDATE DATABASE AND OCCUPANCY DATA
# ============================================================

print("\nVALIDATING LOCKER DATA")
print("--------------------------------")

validation_errors = []
validation_warnings = []

for locker in locations:

    name = locker["name"]

    # 1. Check whether total occupancy matches S/M/L occupancy.

    size_occupied_total = (
        locker["small_occupied"]
        + locker["medium_occupied"]
        + locker["large_occupied"]
    )

    if size_occupied_total != locker["occupied"]:
        validation_errors.append(
            f"{name}: Total occupied parcels ({locker['occupied']}) "
            f"do not match S/M/L total ({size_occupied_total})."
        )

    # 2. Check whether total capacity matches S/M/L capacity.

    size_capacity_total = (
        locker["small_capacity"]
        + locker["medium_capacity"]
        + locker["large_capacity"]
    )

    if size_capacity_total != locker["capacity"]:
        validation_errors.append(
            f"{name}: Total capacity ({locker['capacity']}) "
            f"does not match S/M/L capacity ({size_capacity_total})."
        )

    # 3. Check that no compartment size exceeds its capacity.

    compartment_checks = [
        ("Small", locker["small_occupied"], locker["small_capacity"]),
        ("Medium", locker["medium_occupied"], locker["medium_capacity"]),
        ("Large", locker["large_occupied"], locker["large_capacity"])
    ]

    for size, occupied, capacity in compartment_checks:

        if occupied > capacity:
            validation_errors.append(
                f"{name}: {size} occupancy ({occupied}) "
                f"exceeds capacity ({capacity})."
            )

        if occupied < 0 or capacity < 0:
            validation_errors.append(
                f"{name}: Negative {size} occupancy or capacity."
            )

    # 4. Check overall occupancy against total capacity.

    if locker["occupied"] > locker["capacity"]:
        validation_errors.append(
            f"{name}: Overall occupancy ({locker['occupied']}) "
            f"exceeds total capacity ({locker['capacity']})."
        )

    if locker["occupied"] < 0 or locker["capacity"] < 0:
        validation_errors.append(
            f"{name}: Negative overall occupancy or capacity."
        )

    # 5. Check whether utilisation is mathematically correct.

    if locker["capacity"] > 0:

        calculated_utilisation = (
            locker["occupied"] / locker["capacity"] * 100
        )

        if abs(
            locker["utilisation"] - calculated_utilisation
        ) > 0.11:
            validation_errors.append(
                f"{name}: Reported utilisation "
                f"({locker['utilisation']:.1f}%) does not match "
                f"calculated utilisation "
                f"({calculated_utilisation:.1f}%)."
            )

    elif locker["occupied"] > 0:

        validation_errors.append(
            f"{name}: Parcels exist despite zero total capacity."
        )

    else:
        validation_warnings.append(
            f"{name}: Locker has zero total capacity."
        )

    # 6. Check geographical coordinates.

    latitude = locker["lat"]
    longitude = locker["lon"]

    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        validation_errors.append(
            f"{name}: Invalid geographical coordinates."
        )


# 7. Display validation results.

if validation_errors:

    print("\nVALIDATION FAILED")

    for error in validation_errors:
        print(f"[ERROR] {error}")

    raise ValueError(
        f"Data validation failed with "
        f"{len(validation_errors)} error(s). "
        "Fix the underlying data before proceeding."
    )

else:

    print("[OK] Total occupancy matches S/M/L occupancy.")
    print("[OK] Total capacity matches S/M/L capacity.")
    print("[OK] No compartment exceeds its capacity.")
    print("[OK] Overall occupancy does not exceed capacity.")
    print("[OK] Utilisation percentages are mathematically correct.")
    print("[OK] All geographical coordinates are valid.")

if validation_warnings:

    print("\nWARNINGS:")

    for warning in validation_warnings:
        print(f"[WARNING] {warning}")

else:

    print("[OK] No zero-capacity lockers found.")

print("\nDATA VALIDATION PASSED")
print("--------------------------------")
print(f"Lockers validated: {len(locations)}")


# ============================================================
# 2.2. IDENTIFY CAPACITY IMBALANCES
# ============================================================

print("\nLOCKER CAPACITY ANALYSIS")
print("--------------------------------")

# Overall utilisation thresholds
CRITICAL_THRESHOLD = 85
HIGH_THRESHOLD = 70
LOW_THRESHOLD = 50

critical_lockers = []
high_utilisation_lockers = []
underutilised_lockers = []

for locker in locations:

    utilisation = locker["utilisation"]

    if utilisation >= CRITICAL_THRESHOLD:
        critical_lockers.append(locker)

    elif utilisation >= HIGH_THRESHOLD:
        high_utilisation_lockers.append(locker)

    elif utilisation < LOW_THRESHOLD:
        underutilised_lockers.append(locker)


# Sort lockers by utilisation
critical_lockers.sort(
    key=lambda x: x["utilisation"],
    reverse=True
)

high_utilisation_lockers.sort(
    key=lambda x: x["utilisation"],
    reverse=True
)

underutilised_lockers.sort(
    key=lambda x: x["utilisation"]
)


print(f"Total lockers analysed: {len(locations)}")
print(f"Critical lockers (85%+): {len(critical_lockers)}")
print(f"High utilisation (70-84.9%): {len(high_utilisation_lockers)}")
print(f"Underutilised lockers (<50%): {len(underutilised_lockers)}")


print("\nCRITICAL LOCKERS")
print("--------------------------------")

if critical_lockers:

    for locker in critical_lockers:

        print(
            f"{locker['name']}: "
            f"{locker['utilisation']:.1f}% "
            f"({locker['occupied']}/{locker['capacity']} parcels)"
        )

else:
    print("No critically utilised lockers found.")


print("\nHIGH-UTILISATION LOCKERS")
print("--------------------------------")

if high_utilisation_lockers:

    for locker in high_utilisation_lockers:

        print(
            f"{locker['name']}: "
            f"{locker['utilisation']:.1f}% "
            f"({locker['occupied']}/{locker['capacity']} parcels)"
        )

else:
    print("No lockers in the high-utilisation range.")


print("\nUNDERUTILISED LOCKERS")
print("--------------------------------")

if underutilised_lockers:

    for locker in underutilised_lockers:

        available = locker["capacity"] - locker["occupied"]

        print(
            f"{locker['name']}: "
            f"{locker['utilisation']:.1f}% "
            f"({locker['occupied']}/{locker['capacity']} parcels), "
            f"{available} available compartments"
        )

else:
    print("No underutilised lockers found.")


# ============================================================
# 2.3. COMPARTMENT-SIZE CAPACITY ANALYSIS
# ============================================================

print("\nCOMPARTMENT-SIZE ANALYSIS")
print("--------------------------------")

compartment_sizes = [
    ("Small", "small_occupied", "small_capacity"),
    ("Medium", "medium_occupied", "medium_capacity"),
    ("Large", "large_occupied", "large_capacity")
]

for size_name, occupied_key, capacity_key in compartment_sizes:

    total_occupied = sum(
        locker[occupied_key] for locker in locations
    )

    total_capacity = sum(
        locker[capacity_key] for locker in locations
    )

    available = total_capacity - total_occupied

    utilisation = (
        total_occupied / total_capacity * 100
        if total_capacity > 0 else 0
    )

    print(
        f"{size_name}: "
        f"{total_occupied}/{total_capacity} occupied, "
        f"{available} available, "
        f"{utilisation:.1f}% utilisation"
    )

print("--------------------------------")



# ============================================================
# 2.4. IDENTIFY COMPARTMENT-SIZE BOTTLENECKS
# ============================================================

print("\nCOMPARTMENT-SIZE BOTTLENECK ANALYSIS")
print("--------------------------------")

SIZE_THRESHOLD = 85
ALTERNATIVE_THRESHOLD = 70
MAX_DISTANCE_KM = 10
MAX_ALTERNATIVES = 3

size_definitions = [
    ("Small", "small_occupied", "small_capacity"),
    ("Medium", "medium_occupied", "medium_capacity"),
    ("Large", "large_occupied", "large_capacity")
]


# ------------------------------------------------------------
# 1. CALCULATE STRAIGHT-LINE DISTANCE
# ------------------------------------------------------------

def calculate_distance_km(lat1, lon1, lat2, lon2):

    earth_radius_km = 6371

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return (
        2
        * earth_radius_km
        * math.asin(math.sqrt(a))
    )


# ------------------------------------------------------------
# 2. FIND BOTTLENECKS AND SUITABLE ALTERNATIVES
# ------------------------------------------------------------

bottlenecks_found = 0
alternatives_found = 0

bottleneck_results = []

for source in locations:

    for size_name, occupied_key, capacity_key in size_definitions:

        occupied = source[occupied_key]
        capacity = source[capacity_key]

        if capacity <= 0:
            continue

        size_utilisation = occupied / capacity * 100

        if size_utilisation < SIZE_THRESHOLD:
            continue

        bottlenecks_found += 1

        alternatives = []

        # Search for nearby lockers with spare capacity
        for destination in locations:

            if destination["id"] == source["id"]:
                continue

            destination_capacity = destination[capacity_key]
            destination_occupied = destination[occupied_key]

            if destination_capacity <= 0:
                continue

            destination_utilisation = (
                destination_occupied
                / destination_capacity
                * 100
            )

            available = (
                destination_capacity - destination_occupied
            )

            # Only consider destinations below the utilisation threshold
            if (
                destination_utilisation >= ALTERNATIVE_THRESHOLD
                or available <= 0
            ):
                continue

            # Validate coordinates before calculating distance
            coordinates = [
                source["lat"],
                source["lon"],
                destination["lat"],
                destination["lon"]
            ]

            if not all(math.isfinite(value) for value in coordinates):
                continue

            distance = calculate_distance_km(
                source["lat"],
                source["lon"],
                destination["lat"],
                destination["lon"]
            )

            if distance <= MAX_DISTANCE_KM:

                alternatives.append({
                    "name": destination["name"],
                    "distance": distance,
                    "available": available,
                    "utilisation": destination_utilisation,
                    "capacity": destination_capacity,
                    "occupied": destination_occupied
                })

        # Rank alternatives by nearest distance
        alternatives.sort(
            key=lambda x: x["distance"]
        )

        bottleneck_results.append({
            "source": source["name"],
            "size": size_name,
            "utilisation": size_utilisation,
            "occupied": occupied,
            "capacity": capacity,
            "alternatives": alternatives
        })


# ------------------------------------------------------------
# 3. DISPLAY RESULTS
# ------------------------------------------------------------

if bottlenecks_found == 0:

    print(
        f"\nNo compartment-size bottlenecks found "
        f"above {SIZE_THRESHOLD}% utilisation."
    )

else:

    print(
        f"\nBottlenecks identified: {bottlenecks_found}"
    )

    for result in bottleneck_results:

        print("\n--------------------------------")
        print(
            f"BOTTLENECK: {result['source']} "
            f"- {result['size']}"
        )

        print(
            f"Utilisation: {result['utilisation']:.1f}% "
            f"({result['occupied']}/{result['capacity']})"
        )

        alternatives = result["alternatives"]

        if alternatives:

            print(
                f"\nNearby alternatives "
                f"(within {MAX_DISTANCE_KM} km):"
            )

            for alternative in alternatives[:MAX_ALTERNATIVES]:

                alternatives_found += 1

                print(
                    f"  - {alternative['name']}"
                )

                print(
                    f"    Distance: "
                    f"{alternative['distance']:.2f} km"
                )

                print(
                    f"    Available {result['size'].lower()} "
                    f"compartments: {alternative['available']}"
                )

                print(
                    f"    Current utilisation: "
                    f"{alternative['utilisation']:.1f}%"
                )

        else:

            print(
                f"\nNo suitable alternative found "
                f"within {MAX_DISTANCE_KM} km."
            )


# ------------------------------------------------------------
# 4. SUMMARY
# ------------------------------------------------------------

print("\n--------------------------------")
print("BOTTLENECK ANALYSIS SUMMARY")
print("--------------------------------")

print(f"Compartment bottlenecks: {bottlenecks_found}")
print(f"Nearby alternatives identified: {alternatives_found}")

if bottlenecks_found == 0:

    print(
        "No immediate compartment-size capacity "
        "intervention is indicated by the current data."
    )

elif alternatives_found > 0:

    print(
        "Potential redistribution opportunities identified. "
        "Further operational checks are required before "
        "recommending actual parcel transfers."
    )

else:

    print(
        "Capacity bottlenecks exist, but no suitable "
        "nearby alternatives were identified."
    )

print("--------------------------------")
print(
    "Note: Distances are straight-line estimates. "
    "Available capacity does not guarantee that parcels "
    "can be transferred operationally."
)
print("--------------------------------")



# ============================================================
# 3. SELECT DEPOT AND ROUTE STOPS
# ============================================================

# The depot is illustrative, not a verified operational depot.
depot_name = "Riga Central"

selected_stops = [
    "Riga Imanta",
    "Riga Purvciems",
    "Salaspils Centre",
    "Riga Zolitude"
]

locker_by_name = {
    locker["name"]: locker
    for locker in locations
}

if depot_name not in locker_by_name:
    raise ValueError(f"Depot '{depot_name}' was not found in the database.")

for stop in selected_stops:
    if stop not in locker_by_name:
        raise ValueError(f"Route stop '{stop}' was not found in the database.")

depot = locker_by_name[depot_name]

route_locations = [
    depot
] + [
    locker_by_name[name]
    for name in selected_stops
]

print("\nSelected route:")
for locker in route_locations:
    print(f" - {locker['name']}")


# ============================================================
# 4. CALCULATE STRAIGHT-LINE DISTANCES
# ============================================================

def haversine_distance(point_a, point_b):

    earth_radius = 6371

    lat1 = math.radians(point_a["lat"])
    lon1 = math.radians(point_a["lon"])

    lat2 = math.radians(point_b["lat"])
    lon2 = math.radians(point_b["lon"])

    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return earth_radius * c


distance_matrix = []

for origin in route_locations:

    row = []

    for destination in route_locations:

        distance = haversine_distance(origin, destination)

        # OR-Tools uses integer distance values.
        row.append(int(distance * 1000))

    distance_matrix.append(row)


# ============================================================
# 5. OPTIMISE THE ROUTE USING OR-TOOLS
# ============================================================

manager = pywrapcp.RoutingIndexManager(
    len(distance_matrix),
    1,
    0
)

routing = pywrapcp.RoutingModel(manager)


def distance_callback(from_index, to_index):

    from_node = manager.IndexToNode(from_index)
    to_node = manager.IndexToNode(to_index)

    return distance_matrix[from_node][to_node]


transit_callback_index = routing.RegisterTransitCallback(
    distance_callback
)

routing.SetArcCostEvaluatorOfAllVehicles(
    transit_callback_index
)

search_parameters = pywrapcp.DefaultRoutingSearchParameters()

search_parameters.first_solution_strategy = (
    routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
)

search_parameters.local_search_metaheuristic = (
    routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
)

search_parameters.time_limit.seconds = 5

solution = routing.SolveWithParameters(search_parameters)


# ============================================================
# 6. EXTRACT AND COMPARE ROUTES
# ============================================================

if solution:

    index = routing.Start(0)
    optimised_route_indices = []

    while not routing.IsEnd(index):

        node = manager.IndexToNode(index)
        optimised_route_indices.append(node)

        index = solution.Value(
            routing.NextVar(index)
        )

    optimised_route_indices.append(
        manager.IndexToNode(index)
    )

    optimised_route = [
        route_locations[i]
        for i in optimised_route_indices
    ]

else:
    raise RuntimeError("OR-Tools could not find a route.")


def calculate_route_distance(route):

    total_distance = 0

    for i in range(len(route) - 1):

        total_distance += haversine_distance(
            route[i],
            route[i + 1]
        )

    return total_distance


original_route = route_locations + [depot]

original_distance = calculate_route_distance(original_route)
optimised_distance = calculate_route_distance(optimised_route)

if original_distance > 0:

    improvement = (
        (original_distance - optimised_distance)
        / original_distance
    ) * 100

else:
    improvement = 0


print("\nROUTE RESULTS")
print("--------------------------------")
print(f"Original distance:   {original_distance:.2f} km")
print(f"Optimised distance:  {optimised_distance:.2f} km")
print(f"Estimated reduction: {improvement:.1f}%")

print("\nOptimised route:")
for i, locker in enumerate(optimised_route):
    print(f"{i + 1}. {locker['name']}")


# ============================================================
# 7. CREATE OCCUPANCY RINGS
# ============================================================


def create_occupancy_ring(label, occupied, capacity):

    if capacity == 0:
        percentage = 0
        display = "--"
    else:
        percentage = min(100, occupied / capacity * 100)
        display = f"{percentage:.0f}%"

    if percentage >= 85:
        colour = "#dc3545"
    elif percentage >= 70:
        colour = "#f0ad4e"
    else:
        colour = "#28a745"

    return f"""
        <div style="
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 60px;
        ">

            <div style="
                width: 50px;
                height: 50px;
                border-radius: 50%;

                background: conic-gradient(
                    {colour} {percentage}%,
                    #d9dee5 {percentage}% 100%
                );

                display: flex;
                align-items: center;
                justify-content: center;
            ">

                <div style="
                    width: 38px;
                    height: 38px;
                    border-radius: 50%;
                    background: white;

                    display: flex;
                    align-items: center;
                    justify-content: center;

                    font-size: 11px;
                    font-weight: bold;
                    color: #222;
                ">
                    {display}
                </div>

            </div>

        </div>
    """



# ============================================================
# 8. CREATE THE NATIONAL MAP
# ============================================================

map_object = folium.Map(
    location=[56.95, 24.6],
    zoom_start=7,
    tiles=None
)

folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    attr="Esri",
    name="Street map",
    overlay=False,
    control=True
).add_to(map_object)


# ============================================================
# 9. CREATE LOCKER MARKERS AND DETAILED POPUPS
# ============================================================

route_stop_names = set(selected_stops)

for locker in locations:

    # Overall occupancy colour

    utilisation = min(100, max(0, locker["utilisation"]))

    if utilisation >= 85:
        colour = "#dc3545"
    elif utilisation >= 70:
        colour = "#f0ad4e"
    else:
        colour = "#28a745"

    # Detailed S/M/L rings

    small_ring = create_occupancy_ring(
        "S",
        locker["small_occupied"],
        locker["small_capacity"]
    )

    medium_ring = create_occupancy_ring(
        "M",
        locker["medium_occupied"],
        locker["medium_capacity"]
    )

    large_ring = create_occupancy_ring(
        "L",
        locker["large_occupied"],
        locker["large_capacity"]
    )

    # Border identifies depot and selected route stops

    if locker["name"] == depot_name:
        border_colour = "#1769aa"
        border_width = "3px"

    elif locker["name"] in route_stop_names:
        border_colour = "#e58b20"
        border_width = "3px"

    else:
        border_colour = "#777777"
        border_width = "1px"

    # One clean circular marker

    marker_html = f"""
    <div class="locker-marker" style="
        width: 38px;
        height: 38px;
        border-radius: 50%;

        background: conic-gradient(
            {colour} {utilisation}%,
            #d9dee5 {utilisation}% 100%
        );

        border: {border_width} solid {border_colour};

        display: flex;
        align-items: center;
        justify-content: center;

        box-shadow: 0 1px 4px rgba(0,0,0,0.3);
    ">

        <div style="
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: white;

            display: flex;
            align-items: center;
            justify-content: center;

            font-size: 10px;
            font-weight: bold;
            color: #222;
        ">
            {utilisation:.0f}%
        </div>

    </div>
    """

    # Detailed popup with all occupancy figures

    
    # Detailed popup with all occupancy figures

    popup_html = f"""
    <div style="
        min-width: 280px;
        font-family: Arial, sans-serif;
        color: #222;
        padding: 5px;
    ">

        <h3 style="margin: 0 0 8px 0; font-size: 18px;">
            {locker['name']}
        </h3>

        <div style="font-size: 13px; color: #666; line-height: 1.6;">
            <b>Region:</b> {locker['region']}<br>
            <b>Locker code:</b> {locker['code']}
        </div>

        <div style="
            margin-top: 15px;
            margin-bottom: 15px;
            padding: 12px;
            background: #f3f5f7;
            border-radius: 8px;
        ">

            <div style="font-size: 12px; color: #666; font-weight: bold;">
                OVERALL OCCUPANCY
            </div>

            <div style="
                font-size: 25px;
                font-weight: bold;
                color: {colour};
                margin-top: 4px;
            ">
                {locker['occupied']} / {locker['capacity']}
                ({utilisation:.1f}%)
            </div>

        </div>

        <div style="
            font-size: 13px;
            font-weight: bold;
            margin-bottom: 15px;
            text-align: center;
        ">
            COMPARTMENT OCCUPANCY
        </div>

        <div style="
            display: flex;
            justify-content: space-around;
            align-items: flex-start;
            text-align: center;
            gap: 8px;
        ">

            <div style="
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                text-align: center;
            ">
                <div style="margin-bottom: 10px;">
                    {small_ring}
                </div>

                <div style="
                    font-size: 13px;
                    font-weight: bold;
                    text-align: center;
                    width: 100%;
                ">
                    SMALL
                </div>

                <div style="
                    font-size: 13px;
                    color: #666;
                    margin-top: 5px;
                    text-align: center;
                    width: 100%;
                ">
                    {locker['small_occupied']} / {locker['small_capacity']}
                </div>
            </div>

            <div style="
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                text-align: center;
            ">
                <div style="margin-bottom: 10px;">
                    {medium_ring}
                </div>

                <div style="
                    font-size: 13px;
                    font-weight: bold;
                    text-align: center;
                    width: 100%;
                ">
                    MEDIUM
                </div>

                <div style="
                    font-size: 13px;
                    color: #666;
                    margin-top: 5px;
                    text-align: center;
                    width: 100%;
                ">
                    {locker['medium_occupied']} / {locker['medium_capacity']}
                </div>
            </div>

            <div style="
                flex: 1;
                display: flex;
                flex-direction: column;
                align-items: center;
                text-align: center;
            ">
                <div style="margin-bottom: 10px;">
                    {large_ring}
                </div>

                <div style="
                    font-size: 13px;
                    font-weight: bold;
                    text-align: center;
                    width: 100%;
                ">
                    LARGE
                </div>

                <div style="
                    font-size: 13px;
                    color: #666;
                    margin-top: 5px;
                    text-align: center;
                    width: 100%;
                ">
                    {locker['large_occupied']} / {locker['large_capacity']}
                </div>
            </div>

        </div>

    </div>
    """


    # Add marker and popup to the map

    folium.Marker(
        location=[locker["lat"], locker["lon"]],
        tooltip=locker["name"],

        popup=folium.Popup(
            popup_html,
            max_width=320
        ),

        icon=folium.DivIcon(
            html=marker_html,
            icon_size=(44, 44),
            icon_anchor=(22, 22)
        )

    ).add_to(map_object)


# ============================================================
# 10. ADD ORIGINAL AND OPTIMISED ROUTES
# ============================================================

original_route_coordinates = [
    [locker["lat"], locker["lon"]]
    for locker in original_route
]

optimised_route_coordinates = [
    [locker["lat"], locker["lon"]]
    for locker in optimised_route
]


# Original route, hidden by default.

original_route_layer = folium.FeatureGroup(
    name="Original route",
    show=False
)

folium.PolyLine(
    locations=original_route_coordinates,
    color="red",
    weight=4,
    opacity=0.8,
    tooltip=f"Original route: {original_distance:.2f} km"
).add_to(original_route_layer)

original_route_layer.add_to(map_object)


# Optimised route, visible by default.

optimised_route_layer = folium.FeatureGroup(
    name="Optimised route",
    show=True
)

folium.PolyLine(
    locations=optimised_route_coordinates,
    color="green",
    weight=5,
    opacity=0.85,
    tooltip=f"Optimised route: {optimised_distance:.2f} km"
).add_to(optimised_route_layer)

optimised_route_layer.add_to(map_object)


# ============================================================
# 11. ADD A MAP LEGEND
# ============================================================

legend_html = """
<div style="
    position: fixed;
    bottom: 25px;
    left: 25px;
    z-index: 9999;

    background: white;
    padding: 12px 15px;

    border: 1px solid #aaa;
    border-radius: 7px;

    box-shadow: 0 1px 5px rgba(0,0,0,0.25);

    font-family: Arial, sans-serif;
    font-size: 12px;
">

    <b>Locker occupancy</b><br><br>

    <span style="color:#28a745;">●</span>
    Low: below 70%<br>

    <span style="color:#f0ad4e;">●</span>
    Medium: 70-84%<br>

    <span style="color:#dc3545;">●</span>
    High: 85% or above<br>

    <hr style="margin: 7px 0;">

    <b>Ring labels</b><br>
    S = Small compartments<br>
    M = Medium compartments<br>
    L = Large compartments<br>

    <hr style="margin: 7px 0;">

    <b>Marker borders</b><br>
    <span style="color:#1769aa;">●</span>
    Illustrative depot<br>
    <span style="color:#e58b20;">●</span>
    Selected route stop

</div>
"""

map_object.get_root().html.add_child(
    folium.Element(legend_html)
)


# ============================================================
# 12. ADD LAYER CONTROLS AND SAVE MAP
# ============================================================

folium.LayerControl(
    collapsed=False
).add_to(map_object)


# Fit the map to include all active lockers.

all_coordinates = [
    [locker["lat"], locker["lon"]]
    for locker in locations
]

map_object.fit_bounds(all_coordinates)


output_file = project_folder / "delivery_route_map.html"

map_object.save(str(output_file))


# ============================================================
# 13. FINAL OUTPUT
# ============================================================

print("\n--------------------------------")
print("MAP CREATED SUCCESSFULLY")
print("--------------------------------")

print(f"Total lockers displayed: {len(locations)}")
print(f"Map saved to: {output_file}")

print("\nImportant limitations:")
print("- Occupancy figures come from the synthetic Azure SQL dataset.")
print("- Route distances are straight-line estimates, not road distances.")
print("- Riga Central is an illustrative depot assumption.")
print("- The route is a demonstration using selected lockers.")
print("- No parcel pickup/drop-off scheduling is implemented yet.")

print("\nOpen the map with:")
print("start delivery_route_map.html")
