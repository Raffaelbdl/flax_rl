from rl_tools.transformation import linear_interpolation
from rl_tools.transformation import inverse_linear_interpolation


def test_linear_interpolation():
    assert linear_interpolation(0.0, -1.0, 1.0) == -1.0
    assert linear_interpolation(0.5, -2.0, 0.0) == -1.0
    assert linear_interpolation(-0.5, -2.0, 0.0) == -2.0


def test_inverse_linear_interpolation():
    assert inverse_linear_interpolation(0.0, -1.0, 1.0) == 0.5
    assert inverse_linear_interpolation(0.5, -2.0, 0.0) == 1.0
    assert inverse_linear_interpolation(-0.5, -2.0, 0.0) == 0.75
