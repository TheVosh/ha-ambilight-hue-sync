"""Tests for Philips JointSpace Ambilight normalization and mapping."""

from custom_components.hue_entertainment.jointspace import (
    map_points_to_channels,
    measured_points,
    parse_topology,
)


def test_dynamic_topology_and_edge_positions() -> None:
    topology = parse_topology({"left": 2, "top": 3, "right": 1, "bottom": 0})
    points = measured_points(
        {
            "layer1": {
                "left": {"0": {"r": 1, "g": 2, "b": 3}, "1": {"r": 4, "g": 5, "b": 6}},
                "top": {"0": {"r": 7, "g": 8, "b": 9}},
            }
        },
        topology,
    )
    assert topology == {"left": 2, "top": 3, "right": 1, "bottom": 0}
    assert points[0].position == (-1.0, -1.0, 0.0)
    assert points[1].position == (-1.0, 1.0, 0.0)


def test_reverse_edge_and_nearest_channel_mapping() -> None:
    topology = parse_topology({"left": 0, "top": 2, "right": 0, "bottom": 0})
    points = measured_points(
        {"layer1": {"top": {"0": {"r": 10, "g": 20, "b": 30}, "1": {"r": 40, "g": 50, "b": 60}}}},
        topology,
        {"top"},
    )
    colors = map_points_to_channels(points, {1: (-1, 1, 0), 2: (1, 1, 0)})
    assert [(color.channel_id, color.r, color.g, color.b) for color in colors] == [
        (1, 40 * 257, 50 * 257, 60 * 257),
        (2, 10 * 257, 20 * 257, 30 * 257),
    ]


def test_invalid_or_missing_measured_layer_is_ignored() -> None:
    topology = parse_topology({"left": 1})
    assert measured_points({}, topology) == []
    assert measured_points({"layer1": {"left": {"0": {"r": "bad"}}}}, topology) == []
