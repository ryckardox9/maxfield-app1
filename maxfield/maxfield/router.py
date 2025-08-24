#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""
Ingress Maxfield - router.py

GNU Public License
http://www.gnu.org/licenses/
Copyright(C) 2020
"""

import itertools
import functools
import numpy as np

# Tentativa de OR-Tools; se não houver, caímos num modo simples
try:
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2  # type: ignore
    HAS_ORTOOLS = True
except Exception:
    HAS_ORTOOLS = False

# walking speed (m/s)
_WALKSPEED = 1

# Seconds required to communicate completed links.
_COMMTIME = 30

# Seconds required to create a link
_LINKTIME = 30


def time_callback(origins_dists, count_cut_origins):
    """
    Creates a callback to get total time between two portals.
    total = action(A) + travel(A,B)
    """

    def action_time(node):
        if node == 0:  # dummy depot
            return 0
        return count_cut_origins[node - 1] * _LINKTIME

    def travel_time(from_node, to_node):
        return origins_dists[from_node][to_node] / _WALKSPEED

    _total_time = {}
    for from_node in range(len(origins_dists)):
        _total_time[from_node] = {}
        for to_node in range(len(origins_dists)):
            _total_time[from_node][to_node] = action_time(from_node) + travel_time(from_node, to_node)

    def time_evaluator(manager, from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return _total_time[from_node][to_node]

    return time_evaluator


class Router:
    """
    Vehicle routing para determinar assignments.
    Se OR-Tools indisponível, usa fallback simples (round-robin).
    """

    def __init__(self, graph, portals_dists, num_agents=1,
                 max_route_solutions=100, max_route_runtime=60):
        self.graph = graph
        self.portals_dists = portals_dists
        self.num_agents = num_agents
        self.max_route_solutions = max_route_solutions
        self.max_route_runtime = max_route_runtime

        # links e origens em ordem
        link_orders = [self.graph.edges[link]['order'] for link in self.graph.edges]
        self.ordered_links = [link for _, link in sorted(zip(link_orders, list(self.graph.edges)))]
        self.ordered_origins = [link[0] for link in self.ordered_links]
        self.ordered_links_depends = [graph.edges[link]['depends'] for link in self.ordered_links]

    def route_agents(self):
        """
        Retorna lista de assignments:
        [{'agent', 'location', 'arrive', 'link', 'depart'}, ...]
        """

        # Fallback sem OR-Tools: distribuição simples round-robin
        if not HAS_ORTOOLS:
            assignments = []
            t = 0
            # Estratégia simples: segue a ordem global de links e alterna agente
            for i, link in enumerate(self.ordered_links):
                agent = i % self.num_agents
                origin, dest = link
                arrive = t
                depart = arrive + _LINKTIME
                assignments.append({
                    'agent': agent,
                    'location': origin,
                    'arrive': arrive,
                    'link': dest,
                    'depart': depart
                })
                # avança o tempo de forma constante; se quiser,
                # você pode somar deslocamento aproximado aqui
                t += _LINKTIME
            # ordena por chegada (compatível com o restante do pipeline)
            assignments = sorted(assignments, key=lambda k: k['arrive'])
            return assignments

        # ===== Caminho com OR-Tools disponível =====

        # Caso trivial: 1 agente, segue a ordem
        if self.num_agents == 1:
            assignments = []
            for i in range(len(self.ordered_links)):
                if i == 0:
                    arrive = 0
                else:
                    arrive = (depart +
                              self.portals_dists[
                                  self.ordered_links[i - 1][0],
                                  self.ordered_links[i][0]
                              ] // _WALKSPEED)
                depart = arrive + _LINKTIME
                location = self.ordered_links[i][0]
                link = self.ordered_links[i][1]
                assignments.append(
                    {'agent': 0, 'location': location, 'arrive': arrive,
                     'link': link, 'depart': depart})
            return assignments

        # Remove sequências de mesma origem (otimização)
        ordered_cut_origins, count_cut_origins = \
            zip(*[(x, len(list(y))) for x, y in itertools.groupby(self.ordered_origins)])

        # Matriz de distâncias entre as origens no corte
        origins_dists = np.array([[self.portals_dists[o1][o2] for o1 in ordered_cut_origins]
                                  for o2 in ordered_cut_origins])

        # Adiciona "dummy depot" (linha/coluna 0)
        origins_dists = np.hstack((np.zeros((origins_dists.shape[0], 1)), origins_dists))
        origins_dists = np.vstack((np.zeros(origins_dists.shape[1]), origins_dists))
        origins_dists = np.array(origins_dists, dtype=int)

        # Manager e routing
        manager = pywrapcp.RoutingIndexManager(len(origins_dists), self.num_agents, 0)
        routing = pywrapcp.RoutingModel(manager)

        # Callback de tempo
        time_callback_index = routing.RegisterTransitCallback(
            functools.partial(time_callback(origins_dists, count_cut_origins), manager)
        )

        # Custo: tempo total
        routing.SetArcCostEvaluatorOfAllVehicles(time_callback_index)

        # Dimensão de tempo
        routing.AddDimension(time_callback_index, 1000000, 1000000, False, 'time')
        time_dimension = routing.GetDimensionOrDie('time')
        time_dimension.SetGlobalSpanCostCoefficient(100)

        # Respeitar dependências entre grupos
        for i in range(1, len(origins_dists) - 1):
            this_index = manager.NodeToIndex(i)
            next_index = manager.NodeToIndex(i + 1)

            this_link = int(np.sum(count_cut_origins[:i - 1]))
            this_size = count_cut_origins[i - 1]
            next_link = int(np.sum(count_cut_origins[:i]))
            next_size = count_cut_origins[i]

            for linki in range(this_link, this_link + this_size):
                for linkj in range(next_link, next_link + next_size):
                    if ((self.ordered_links[linki] in self.ordered_links_depends[linkj]) or
                            (self.ordered_links[linki][0] in self.ordered_links_depends[linkj])):
                        # Conflito de dependência
                        break
                else:
                    continue
                break
            else:
                routing.solver().Add(
                    (time_dimension.CumulVar(next_index) >= time_dimension.CumulVar(this_index))
                )
                continue

            routing.solver().Add(
                (time_dimension.CumulVar(next_index) >
                 (time_dimension.CumulVar(this_index) +
                  count_cut_origins[i - 1] * _LINKTIME + _COMMTIME))
            )

        # Start em 0 e minimizar total
        for i in range(self.num_agents):
            time_dimension.CumulVar(routing.Start(i)).SetRange(0, 0)
            routing.AddVariableMinimizedByFinalizer(time_dimension.CumulVar(routing.End(i)))

        # Parâmetros de busca
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_parameters.solution_limit = self.max_route_solutions
        search_parameters.time_limit.seconds = self.max_route_runtime
        routing.CloseModelWithParameters(search_parameters)

        # Solução inicial ingênua
        naive_route = [list(range(1, len(origins_dists)))[i::self.num_agents] for i in range(self.num_agents)]
        naive_solution = routing.ReadAssignmentFromRoutes(naive_route, True)

        # Resolve
        solution = routing.SolveFromAssignmentWithParameters(naive_solution, search_parameters)
        if not solution:
            raise ValueError("No valid assignments found")

        # Empacota resultados
        assignments = []
        for agent in range(self.num_agents):
            index = routing.Start(agent)
            index = solution.Value(routing.NextVar(index))
            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                arrive = solution.Min(time_dimension.CumulVar(index))
                linki = int(np.sum(count_cut_origins[:node - 1]))
                for i in range(linki, linki + count_cut_origins[node - 1]):
                    location = self.ordered_links[i][0]
                    link = self.ordered_links[i][1]
                    depart = arrive + _LINKTIME
                    assignments.append({
                        'agent': agent, 'location': location,
                        'arrive': arrive, 'link': link,
                        'depart': depart
                    })
                    arrive = depart
                index = solution.Value(routing.NextVar(index))

        assignments = sorted(assignments, key=lambda k: k['arrive'])
        return assignments
