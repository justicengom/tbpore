"""
Module responsible for clustering tbpore process consensus sequences.
Totally based on https://github.com/mbhall88/head_to_head_pipeline/blob/bcbc84971342a26cd0a9f0ad8df4f01dcf35c01c/analysis/transmission_clustering/eda/clustering.ipynb
"""

from itertools import chain
from pathlib import Path
from typing import List, Set

import networkx as nx
import numpy as np
import pandas as pd

DELIM = ","
PAIR_IDX = ("sample1", "sample2")


class AsymmetrixMatrixError(Exception):
    pass


def load_matrix(fpath, delim: str = DELIM, name: str = "") -> pd.Series:
    matrix = []
    with open(fpath) as instream:
        header = next(instream).rstrip()
        names = np.array(header.split(delim)[1:])
        idx = np.argsort(names)
        sorted_names = names[idx]
        for row in map(str.rstrip, instream):
            # sort row according to the name sorting
            sorted_row = np.array(row.split(delim)[1:], dtype=int)[idx]
            matrix.append(sorted_row)
    sorted_matrix = np.array(matrix)[idx]
    n_samples = len(sorted_names)
    diagonal_is_zero = all(sorted_matrix[i, i] == 0 for i in range(n_samples))
    if not diagonal_is_zero:
        raise AsymmetrixMatrixError("Distance matrix diagonal is not all zero")

    matrix_is_symmetric = np.allclose(sorted_matrix, sorted_matrix.T)
    if not matrix_is_symmetric:
        raise AsymmetrixMatrixError("Distance matrix is not symmetric")

    mx = pd.DataFrame(sorted_matrix, columns=sorted_names, index=sorted_names)
    # remove the lower triangle of the matrix and the middle diagonal
    mx = mx.where(np.triu(np.ones(mx.shape), k=1).astype(bool))
    mx = mx.stack().rename(name).astype(int)
    mx = mx.rename_axis(PAIR_IDX)

    return mx


def matrix_to_graph(
    mx: pd.Series, threshold: int, include_singletons: bool = False
) -> nx.Graph:
    edges = [(s1, s2, dist) for (s1, s2), dist in mx.iteritems() if dist <= threshold]
    graph = nx.Graph()
    graph.add_weighted_edges_from(edges)
    if include_singletons:
        samples = set()
        for u in chain.from_iterable(mx.index):
            if u not in samples:
                graph.add_node(u)
                samples.add(u)
    return graph


def get_clusters(psdm_matrix: Path, clustering_threshold: int) -> List[Set[str]]:
    ont_mtx = load_matrix(psdm_matrix, name="nanopore")
    ont_graph = matrix_to_graph(
        ont_mtx, threshold=clustering_threshold, include_singletons=True
    )
    return list(nx.connected_components(ont_graph))


def get_formatted_clusters(clusters: List[Set[str]]) -> str:
    clusters_as_strs = []
    for cluster_index, cluster in enumerate(clusters):
        cluster_as_str = "\t".join(cluster)
        cluster_description = f"Cluster #{cluster_index+1}:\t{cluster_as_str}"
        clusters_as_strs.append(cluster_description)
    return "\n".join(clusters_as_strs)
