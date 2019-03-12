import unittest
from inferelator_ng.regression import base_regression
import pandas as pd
import numpy as np
import os

class TestBaseRegression(unittest.TestCase):

    def test_scale(self):
        df = pd.DataFrame(np.array([[0, 1], [0, 1]]))
        result = base_regression.BaseRegression._scale(df)
        np.testing.assert_array_almost_equal(result, np.array([[-0.707107, 0.707107], [-0.707107, 0.707107]]), 5)