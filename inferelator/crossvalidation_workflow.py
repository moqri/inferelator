from __future__ import print_function
from builtins import FileExistsError

import os
import warnings
import csv
import types
import copy
import itertools
import numpy as np
import pandas as pd


from inferelator.distributed.inferelator_mp import MPControl
from inferelator.utils import Validator as check
from inferelator import utils
from inferelator import workflow


class CrossValidationManager(object):
    """
    This does cross-validation for workflows
    """

    # Output settings
    _csv_writer = None  # csv.csvwriter
    _csv_header = None  # list[]
    output_file_name = "aupr.tsv"  # str
    _csv_writer_object = csv.writer
    _csv_file_handle = None

    # Grid search parameters
    grid_params = None
    grid_param_values = None

    # Dropin/dropout categories
    dropout_column = None
    dropout_max_size = None
    dropout_seed = None

    dropin_column = None
    dropin_max_size = None
    dropin_seed = None

    # Size subsampling categories
    size_sample_vector = None
    size_sample_stratified_column = None
    size_sample_with_replacement = False
    size_sample_seed = None

    # Workflow storage
    _baseline_workflow = None
    _baseline_output_dir = None
    _baseline_input_dir = None

    @property
    def output_dir(self):
        if self._baseline_workflow is None:
            return self._baseline_output_dir
        else:
            return self._baseline_workflow.output_dir

    @output_dir.setter
    def output_dir(self, path):
        if self._baseline_workflow is not None:
            self._baseline_workflow.output_dir = path
        else:
            self._baseline_output_dir = path

    @property
    def input_dir(self):
        if self._baseline_workflow is None:
            return self._baseline_input_dir
        else:
            return self._baseline_workflow.input_dir

    @input_dir.setter
    def input_dir(self, path):
        if self._baseline_workflow is not None:
            self._baseline_workflow.input_dir = path
        else:
            self._baseline_input_dir = path

    @property
    def workflow(self):
        if self._baseline_workflow is None:
            raise AttributeError("No workflow has been provided")
        return self._baseline_workflow

    @workflow.setter
    def workflow(self, wkf):
        assert check.argument_is_subclass(wkf, workflow.WorkflowBase, allow_none=True)
        if self._baseline_workflow is not None:
            warnings.warn("Replacing stored workflow with a new workflow")
        self._baseline_workflow = wkf

    def __init__(self, workflow_object=None):
        self.workflow = workflow_object

    def add_gridsearch_parameter(self, param_name, param_vector):
        """
        Set a parameter to search through by exhaustive grid search

        :param param_name: str
            The workflow parameter to change for each run
        :param param_vector: list, tuple
            An iterable with values to use for the parameter
        """

        if self.grid_param_values is None:
            self.grid_param_values = {}

        if self.grid_params is None:
            self.grid_params = []

        self.grid_params.append(param_name)
        self.grid_param_values[param_name] = param_vector

    def add_grouping_dropout(self, metadata_column_name, group_size=None, seed=42):
        """
        Drop each group (defined by a metadata column) and run modeling on all of the other groups

        :param metadata_column_name: str
        :param group_size: int
        :param seed: int
        """

        self.dropout_column = metadata_column_name
        self.dropout_max_size = group_size
        self.dropout_seed = seed

    def add_grouping_dropin(self, metadata_column_name, group_size=None, seed=42):
        """

        :param metadata_column_name: str
        :param group_size: int
        :param seed: int
        """

        self.dropin_column = metadata_column_name
        self.dropin_max_size = group_size
        self.dropin_seed = seed

    def add_size_subsampling(self, size_vector, stratified_column_name=None, with_replacement=False, seed=42):
        """

        :param size_vector: list, tuple
            An iterable with numeric ratios for downsampling.
        :param stratified_column_name: str
            Set this to stratify sampling (to maintain group size ratios)
        :param with_replacement: bool
            Do sampling with or without replacement
        :param seed: int
            Initial seed for selecting observations (this is not the same as the seed passed to the workflow)
        """

        try:
            [check.argument_numeric(val, low=0, high=1) for val in size_vector]
        except ValueError as err:
            utils.Debug.vprint("Size sampling parameter error: {err}".format(err=str(err)), level=0)
            raise

        self.size_sample_vector = size_vector
        self.size_sample_stratified_column = stratified_column_name
        self.size_sample_with_replacement = with_replacement
        self.size_sample_seed = seed

    def run(self):

        # Create output path
        self._harmonize_paths()
        # Open a CSV file handle
        self._create_writer()
        # Load and check data into a workflow object
        self._initial_data_load()
        self._check_metadata()
        self._check_grid_search_params_exist()
        # Run base grid search
        self._grid_search()

        # Run size sampling
        if self.size_sample_vector is not None:
            self._size_cv()

        # Run dropin
        if self.dropin_column is not None:
            self._dropin_cv()

        # Run dropout
        if self.dropout_column is not None:
            self._dropout_cv()

        self._destroy_writer()

    def append_to_path(self, var_name, to_append):
        """
        Add a string to an existing path variable in class
        """

        # Get the path
        path = getattr(self, var_name)

        # If neither are set just die
        if path is None:
            raise ValueError("Cannot append {to_append} to {var_name} (Which is None)".format(to_append=to_append,
                                                                                              var_name=var_name))
        setattr(self, var_name, os.path.join(path, to_append))

    def _create_writer(self):
        """
        Create a CSVWriter and stash it in self.writer
        """

        if MPControl.is_master:
            # Create a CSV header from grid search param names
            self._csv_header = copy.copy(self.grid_params) if self.grid_params is not None else []

            # Add Test & Value columns for dropouts/etc
            self._csv_header.extend(["Test", "Value", "Num_Obs"])

            # Also add the metric
            self._csv_header.append(self.workflow.metric)

            # Create a CSV writer
            self._create_output_path()
            self._open_csv_handle()

            self._csv_writer = self._csv_writer_object(self._csv_file_handle,
                                                       delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE)

            # Write the header line
            self._csv_writer.writerow(self._csv_header)

    def _destroy_writer(self):
        """
        Delete the CSVWriter and close the file handle
        """
        if MPControl.is_master:
            self._csv_file_handle.close()
            self._csv_writer = None

    def _harmonize_paths(self):
        """
        If _baseline is set, copy it to the workflow
        If _baseline is not set, copy to it from the workflow
        """
        if self._baseline_output_dir is None and self.workflow.output_dir is None:
            raise ValueError("No output path has been provided to either crossvalidation or workflow")
        elif self._baseline_output_dir is None:
            self._baseline_output_dir = self.workflow.output_dir
        elif self.workflow.output_dir is None:
            self.workflow.output_dir = self._baseline_output_dir

        try:
            check.argument_subpath(self.workflow.output_dir, self._baseline_output_dir)
        except ValueError:
            warnings.warn("Workflow output path is {p}; resetting to {a}".format(a=self.workflow.output_dir,
                                                                                 p=self._baseline_output_dir))
            self.workflow.output_dir = self._baseline_output_dir

        if self._baseline_input_dir is None and self.workflow.input_dir is None:
            raise ValueError("No input path has been provided to either crossvalidation or workflow")

        if self._baseline_input_dir is None:
            self._baseline_input_dir = self.workflow.input_dir
        if self.workflow.input_dir is None:
            self.workflow.input_dir = self._baseline_input_dir

    def _open_csv_handle(self):
        """
        Open and return a file handle to the CSV output file
        """
        csv_file_name = os.path.join(self.output_dir, self.output_file_name)
        self._csv_file_handle = open(csv_file_name, mode="w", buffering=1)

    def _create_output_path(self):
        """
        Create the output path
        """
        self.output_dir = os.path.abspath(os.path.expanduser(self.output_dir))

        try:
            os.makedirs(self.output_dir)
        except FileExistsError:
            pass

    def _initial_data_load(self):
        """
        Load data into the workflow
        """
        # Load data with the baseline get_data() function
        self.workflow.get_data()

        # Blow up the get_data() function so that it doesn't get re-run
        def mock_get_data(slf):
            pass

        self.workflow.get_data = types.MethodType(mock_get_data, self.workflow)

    def _get_workflow_copy(self):
        """
        Copies and returns the workflow which has loaded data
        """

        return copy.deepcopy(self.workflow)

    def _grid_search(self, test=None, value=None, mask_function=None):
        """

        :param test: str
        :param value: str
        :param mask_function: func
            A function which produces a pd.Series(bool) [N] mask for selecting observations
        :return:
        """

        if test is not None:
            utils.Debug.vprint("Grid search for  {t} [{v}]".format(t=test, v=value))

        # This is unpacked in the same order that is used in the header
        ordered_unpack = [self.grid_param_values[param] for param in self.grid_params]

        for param_values in itertools.product(*ordered_unpack):
            params = zip(self.grid_params, param_values)
            csv_line = []
            output_path = []

            # Get the workflow and set the CV parameters
            cv_workflow = self._get_workflow_copy()
            cv_workflow.create_output_dir()

            # Drop any observations which are False in the mask (if set)
            if mask_function is not None:
                mask = mask_function()
                cv_workflow.expression_matrix.drop(cv_workflow.expression_matrix.columns[~mask], axis=1, inplace=True)
                cv_workflow.meta_data.drop(cv_workflow.meta_data.index[~mask], axis=0, inplace=True)
                n_obs = mask.sum()
            else:
                n_obs = cv_workflow.meta_data.shape[0]

            for name, param_value in params:
                csv_line.append(param_value)
                output_path.append(str(name) + "_" + str(param_value))
                setattr(cv_workflow, name, param_value)
                utils.Debug.vprint("Setting crossvalidation param {p} to {v}".format(p=name, v=param_value), level=0)

            # Set the parameters into the output path
            if test is not None:
                output_path = "_".join(map(str, output_path + [test, value]))

            else:
                output_path = "_".join(map(str, output_path))

            # Run the workflow
            cv_workflow.append_to_path("output_dir", output_path)
            utils.Debug.vprint("Writing results to {p} ".format(p=cv_workflow.output_dir), level=1)
            result = cv_workflow.run()
            csv_line.extend([test, value, n_obs, result.score])

            if MPControl.is_master:
                self._csv_writer.writerow(csv_line)

            del cv_workflow

    def _check_grid_search_params_exist(self):
        """
        Determine if the parameters for grid search are workflow parameters
        """

        for param in self.grid_params:
            if hasattr(self.workflow, param):
                pass
            else:
                raise ValueError("Parameter {p} for GridCV does not appear to be a valid parameter".format(p=param))

    def _check_metadata(self):
        """
        Make sure that any set metadata columns exist after loading
        """

        if self.dropout_column is not None:
            self._check_metadata_column_exists(self.dropout_column)
        if self.dropin_column is not None:
            self._check_metadata_column_exists(self.dropin_column)
        if self.size_sample_stratified_column is not None:
            self._check_metadata_column_exists(self.size_sample_stratified_column)

    def _check_metadata_column_exists(self, col_name):
        """
        Check to make sure the metadata column exists

        :param col_name: str
        """

        if col_name in self.workflow.meta_data.columns:
            return True
        else:
            raise ValueError("Column {col} is not present in the loaded metadata".format(col=col_name))

    def _dropout_cv(self):
        """
        Run grid search on all data minus one group at a time
        """

        meta_data = self.workflow.meta_data.copy()
        col = self.dropout_column
        max_size = self.dropout_max_size

        unique_groups = meta_data[col].unique().tolist()

        for i, group in enumerate(unique_groups):
            rgen = np.random.RandomState(self.dropin_seed + i)

            def mask_function():
                include_mask = meta_data[col] != group
                if max_size is None:
                    return include_mask
                else:
                    # For each factor in the stratified column
                    for g in unique_groups:
                        include_mask = include_mask & group_index(meta_data, col, g, rgen=rgen, max_size=max_size)

            self._grid_search(test="dropin", value="group", mask_function=mask_function)

    def _dropin_cv(self):
        """
        Run grid search on one group from the data at a time
        """

        meta_data = self.workflow.meta_data.copy()
        col = self.dropin_column
        max_size = self.dropin_max_size

        unique_groups = meta_data[col].unique().tolist()

        for i, group in enumerate(unique_groups):
            rgen = np.random.RandomState(self.dropin_seed + i)

            def mask_function():
                if max_size is None:
                    return meta_data[col] == group
                else:
                    return group_index(meta_data, col, group, rgen=rgen, max_size=max_size)

            self._grid_search(test="dropin", value="group", mask_function=mask_function)

    def _size_cv(self):
        """
        Run grid search on a subset of the data
        """

        for i, size_ratio in enumerate(self.size_sample_vector):
            rgen = np.random.RandomState(self.size_sample_seed + i)
            meta_data = self.workflow.meta_data.copy()

            if self.size_sample_stratified_column is not None:
                strat_col = self.size_sample_stratified_column

                def data_masker():
                    unique_groups = meta_data[strat_col].unique().tolist()
                    data_mask = pd.Index(False, index=meta_data.index)

                    # For each factor in the stratified column
                    for group in unique_groups:
                        data_mask = data_mask | group_index(meta_data, strat_col, group, size_ratio=size_ratio,
                                                            rgen=rgen)

                    return data_mask

            else:

                def data_masker():
                    n_obs = meta_data.shape[0]
                    size = max(int(n_obs * size_ratio), 1)
                    size_mask = [True] * size + [False] * (n_obs - size)
                    rgen.shuffle(size_mask)
                    return pd.Series(size_mask, index=meta_data.index)

            self._grid_search(test="size", value=str(size_ratio), mask_function=data_masker)


def group_index(meta, meta_col, group, size_ratio=None, rgen=None, max_size=None):
    rgen = rgen if rgen is not None else np.random

    # Find the observations in this group
    group_mask = meta[meta_col] == group
    n_group = group_mask.sum()

    if n_group == 0:
        return group_mask

    # Decide how big to make the group (size * size_ratio)
    if size_ratio is not None:
        size = max(int(n_group * size_ratio), 1)
    else:
        size = max(n_group, 1)

    # Set max_size if that argument has been provided
    if max_size is not None:
        size = min(size, max_size)

    if size != n_group:
        size_mask = [True] * size + [False] * (n_group - size)
        rgen.shuffle(size_mask)
        group_mask.loc[group_mask] = size_mask

    return group_mask
