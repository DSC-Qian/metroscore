import warnings

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox

import metroscore.analysis as mna
import metroscore.constants as mconstants
import metroscore.network_builder as network_builder
import metroscore.service_areas as service_areas
import metroscore.utils as mutils


class Metroscore:
    def __init__(self, name: str, C: float):
        """
        Initialize the Metroscore object.

        Args:
            name (str): The name of the place for analysis.
            C (float): Scaling factor used in metroscore calculation.
            traffic_damper (float): Dampening factor for traffic speeds. Defaults to 0.3.

        Returns:
            Metroscore: Instance of Metroscore object
        """
        self.name = name
        self.C = C

    def build_drive(self, traffic_damper: float = 0.3):
        """
        Build the drive network.

        Args:
            traffic_damper (float): Dampening factor for traffic speeds. Defaults to 0.3.

        Returns:
            Metroscore: Instance of Metroscore object with configured m._drive_graph object.
        """

        # Initialize the drive graph
        self._drive_graph = ox.graph_from_place(
            query=self.name,
            retain_all=False,
            truncate_by_edge=True,
            simplify=True,
            network_type="drive",
        )
        self._drive_graph = ox.project_graph(self._drive_graph)
        edge_travel_speeds = mutils.get_dampened_speeds_per_road_type(
            self._drive_graph,
            traffic_damper,
        )
        self._drive_graph = ox.add_edge_speeds(
            self._drive_graph,
            hwy_speeds={
                k: np.mean(v) * mconstants.MILE_TO_KM * 0.3
                for k, v in edge_travel_speeds.items()
            },
        )
        self._drive_graph = ox.add_edge_travel_times(self._drive_graph)
        return self

    def build_transit(self, **kwargs):
        """
        Build the transit network.

        Args:
            **kwargs: Path to the GTFS feed for each transit mode specified by the user.

        Returns:
            Metroscore: Instance of Metroscore object with configured m._transit_graph object.
        """
        if not kwargs:
            warnings.warn(
                "No GTFS feeds provided. Transit network will be built with only walking."
            )
        # Initialize transit graph
        walk_graph = network_builder.build_walk_graph(self.name)
        gtfs_feeds = {
            k: network_builder.load_gtfs_feed(v) for k, v in kwargs.items() if v
        }
        (
            self._transit_graph,
            self._timetable,
        ) = network_builder.load_network_and_timetable(
            walk=walk_graph,
            **gtfs_feeds,
        )
        return self

    def save(self, path: str):
        raise NotImplementedError("Saving Metroscore objects is not yet implemented.")

    def load(self, path: str):
        raise NotImplementedError("Loading Metroscore objects is not yet implemented")

    def compute(
        self,
        locations: list,
        time_of_days: list,
        cutoffs: list,
        overwrite: bool = False,
    ):
        """
        Compute the metroscores.

        Args:
            locations (list): List of (lat, lon) coordinates to use as test locations.
            time_of_days (list): List of times (in seconds after midnight) to use as departure times.
            cutoffs (list): List of times (in seconds) to use as travel times.
            overwrite (bool): Boolean used to determine if results should overwrite existing ones.

        Returns:
            pandas.DataFrame: DataFrame with 4 columns: location, time_of_day, cutoff, and metroscore.
        """
        if not overwrite and self._results:
            raise ValueError(
                "Results already exist. Set overwrite to True to overwrite."
            )

        transitshed = self.__compute_transitshed(locations, time_of_days, cutoffs)
        driveshed = self.__compute_driveshed(locations, time_of_days, cutoffs)

        self._results = mna.compute_metroscore(
            transitshed,
            driveshed,
            bonus_weight=self.C,
        )
        return self._results

    def __compute_transitshed(self, locations: list, time_of_days: list, cutoffs: list):
        """
        Compute the transitshed.

        Args:
            locations (list): List of (lat, lon) coordinates to use as test locations.
            time_of_days (list): List of times (in seconds after midnight) to use as departure times.
            cutoffs (list): List of times (in seconds) to use as travel times.

        Returns:
            geopandas.GeoDataFrame: DataFrame with 4 columns: location, time_of_day, cutoff, and transit areas (MultiPolygons)
        """
        if not self._transit_graph:
            raise ValueError(
                "No transit network found. Please run the build_transit method first."
            )

        closest_node_list = mutils.get_closest_node(
            start_points=locations,
            graph=self._transit_graph,
        )

        # TODO: deduplicate closest points, as they may be the same for different locations

        # Compute the service areas
        # TODO: needs to happen for all locations (origin_id) and start times
        sp = service_areas.time_dependent_djikstra(
            G=self._transit_graph,
            timetable=self._timetable,
            start_time=official_start_time,
            origin_id=closest_node,
        )
        travel_times_from_origin = dict(
            map(
                lambda x: (x[0], x[1] - official_start_time),
                filter(lambda x: x[1] != np.inf, sp.items()),
            )
        )
        transit_areas = service_areas.get_transit_areas(
            travel_times_from_origin,
            cutoffs,
            ox.graph_to_gdfs(self._transit_graph, edges=False),
        )
        transit_areas = (
            gpd.GeoDataFrame(transit_areas.sort_index(ascending=False))
            .reset_index(drop=False)
            .rename(columns={"index": "cutoffs", 0: "geometry"})
        )
        return transit_areas.set_geometry("geometry")

    def __compute_driveshed(self, locations: list, time_of_days: list, cutoffs: list):
        """
        Compute the driveshed.

        Args:
            locations (list): List of (lat, lon) coordinates to use as test locations.
            time_of_days (list): List of times (in seconds after midnight) to use as departure times.
            cutoffs (list): List of times (in seconds) to use as travel times.

        Returns:
            geopandas.GeoDataFrame: DataFrame with 4 columns: location, time_of_day, cutoff, and drive areas (MultiPolygons)
        """
        if not self._drive_graph:
            raise ValueError(
                "No drive network found. Please run the build_drive method first."
            )

        closest_node_list = mutils.get_closest_node(
            start_points=locations,
            graph=self._drive_graph,
        )

        # Compute the service areas
        # TODO: needs to happen for all locations (source)
        time_to_all_paths = nx.single_source_dijkstra_path_length(
            G=self._drive_graph,
            cutoff=official_start_time + max(cutoffs),
            source=drive_closest_node,
            weight="travel_time",
        )
        drive_areas = msa.get_transit_areas(
            {k: v + drive_time_headstart for k, v in time_to_all_paths.items()},
            cutoffs,
            ox.graph_to_gdfs(drive_network, edges=False),
        )
        drive_areas = (
            gpd.GeoDataFrame(drive_areas.sort_index(ascending=False))
            .reset_index(drop=False)
            .rename(columns={"index": "cutoffs", 0: "geometry"})
        )
        return drive_areas.set_geometry("geometry")

    def get_score(self, location, time_of_day, cutoff):
        """
        Get a single metroscore.

        Args:
            location: Location coordinate (lat, lon).
            time_of_day: Time of day (in seconds after midnight).
            cutoff: Travel duration (in seconds).

        Returns:
            float: Single metroscore value.
        """
        if not self._results:
            raise ValueError("No results found. Please run the compute method first.")
        return self._results.iloc[(location, time_of_day, cutoff)].metroscore

    def list_scores(self, locations=None, time_of_days=None, cutoffs=None):
        """
        Get all metroscores with matching locations, time_of_days, and cutoffs.

        Args:
            locations (list, optional): List of (lat, lon) coordinates. Defaults to None.
            time_of_days (list, optional): List of times (in seconds after midnight). Defaults to None.
            cutoffs (list, optional): List of times (in seconds). Defaults to None.

        Returns:
            pandas.DataFrame: DataFrame with metroscores matching the specified criteria.
        """
        if not self._results:
            raise ValueError("No results found. Please run the compute method first.")
        return self._results.loc[locations, time_of_days, cutoffs][["metroscore"]]

    def slice_results(self, by: str, agg: str):
        """
        Slice and aggregate metroscores across a given dimension.

        Args:
            by (str): Dimension to slice by. Supports "location", "time_of_day", "cutoff", or "all".
            agg (str): Aggregation function to use. Supports "mean", "max", "min", and "median".

        Returns:
            pandas.Series or float: Aggregated metroscores across the specified dimension.
        """
        if not self._results:
            raise ValueError("No results found. Please run the compute method first.")
        raise NotImplementedError("Slicing and aggregation is not yet implemented.")
