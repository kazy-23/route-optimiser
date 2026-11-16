
from ortools.constraint_solver import pywrapcp, routing_enums_pb2


# 1. Define delivery locations
locations = [
    "Main Depot",
    "Delivery A",
    "Delivery B",
    "Delivery C",
    "Delivery D",
    "Delivery E"
]


# 2. Create a distance matrix (kilometres)
# Rows and columns represent the locations above.
distance_matrix = [
    [0,  8,  12,  15,  10,  18],
    [8,  0,   6,  10,  12,  15],
    [12, 6,   0,   7,   9,  11],
    [15, 10,  7,   0,   5,   8],
    [10, 12,  9,   5,   0,   6],
    [18, 15, 11,   8,   6,   0]
]


# 3. Configure the routing problem
number_of_vehicles = 1
depot = 0

manager = pywrapcp.RoutingIndexManager(
    len(distance_matrix),
    number_of_vehicles,
    depot
)

routing = pywrapcp.RoutingModel(manager)


# 4. Tell the optimiser how to calculate distances
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


# 5. Choose the optimisation strategy
search_parameters = pywrapcp.DefaultRoutingSearchParameters()

search_parameters.first_solution_strategy = (
    routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
)

search_parameters.local_search_metaheuristic = (
    routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
)

search_parameters.time_limit.seconds = 5


# 6. Solve the problem
solution = routing.SolveWithParameters(search_parameters)


# 7. Display the result
if solution:
    index = routing.Start(0)
    total_distance = 0

    print("\nOPTIMISED DELIVERY ROUTE")
    print("-" * 35)

    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        print(locations[node])

        previous_index = index
        index = solution.Value(routing.NextVar(index))

        total_distance += routing.GetArcCostForVehicle(
            previous_index, index, 0
        )

    final_node = manager.IndexToNode(index)
    print(locations[final_node])

    print("-" * 35)
    print(f"Total distance: {total_distance} km")

else:
    print("No solution found.")
