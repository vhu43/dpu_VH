"""
Helper functions for
"""

import importlib
import string
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import optimize, stats

from evolver_manager.fits import fit

# Global plt rcParams for figure plotting
plt.rcParams["text.usetex"] = True  # Allowing Latex rendering of equations
plt.rcParams["figure.figsize"] = (10, 8)  # figure size automatic
plt.rcParams["figure.titlesize"] = "xx-large"


def fit_curve(calibration, model_name, fit_name, sensors):
    """Method to perform curve fit"""
    print(__package__)

    # Obtaining the curve instance dynamically
    class_name = model_name[0].upper() + model_name[1:]
    curve_module = importlib.import_module(f"evolver_manager.fits.{model_name}")
    curve: fit.Fit = getattr(curve_module, class_name)

    # Getting raw data from evolver
    calibration_data = process_vial_data(calibration, sensors)
    if not calibration_data:
        raise FileNotFoundError("No Raw Sets found in Calibration File")

    # Performing fit
    coefficients = []
    p_cov_list = []
    p_err_list = []

    for _, data in calibration_data.items():
        for vial_num, vial in enumerate(range(data["vial_num"])):
            sensor_readings = data["sensor_readings"][vial]  # x
            standards_data = data["standards_data"][vial]  # y
            param_opt, param_cov, *_ = optimize.curve_fit(
                f=curve.equation,
                xdata=sensor_readings,
                ydata=standards_data,
                p0=curve.get_initial_values(sensor_readings, standards_data),
                bounds=curve.get_bounds(sensor_readings, standards_data),
                maxfev=1000000000,
            )
            param_error = 2 * np.sqrt(np.diag(param_cov))
            coefficients.append(np.array(param_opt).tolist())
            p_cov_list.append(param_cov)

            # Printing optimal values with error bars
            print(f"vial {vial_num:<2}")
            print(f"  fitted to: {curve.fit_equation_str}")
            for i in range(len(param_opt)):
                print(
                    (
                        f"  {string.ascii_lowercase[i]}:"
                        f" {param_opt[i]} +\\- {param_error[i]}"
                    )
                )
            print()
            p_err_list.append(param_error)

        fig = graph(
            curve=curve,
            y=data["standards_data"],
            x=data["sensor_readings"],
            coef=coefficients,
            p_cov_list=p_cov_list,
            fit_name=fit_name,
        )

    fit_result = {
        "name": fit_name,
        "coefficients": coefficients,
        "type": model_name,
        "timeFit": time.time(),
        "active": False,
        "params": sensors,
    }

    raw = {
        "coef": coefficients,
        "err": p_err_list,
        "eq": curve.fit_equation_str,
        "data": calibration_data,
    }
    return fit_result, fig, raw


def calculate_bands(curve: fit.Fit, y, x, x_space, coef, pcov, alpha=0.05):
    """An implementation of the Delta Method for calculating variance.

    An implementation of the first-order Delta method for estimating parameter
    variance of a multivariable curve fit.

    Args:
      curve: an instance of the fit object used
      y: an array-like containing the dependent variable used for prediction
      x: an array-like containing the independent variable used for predicting a
        dependent variable to get optimal fit
      x_space: an array-like denoting x-values ploted in the plot
      coef: the optimal fit coefficients found from a curve fitting routine
      pcov: The estimated covariance of the fit
      alpha: the power desired. Defaults to 0.05

    returns:
      A 2-tuple of array-like containing the confidence and prediction distance,
      respectively, for a given critical value
    """
    f_prime = curve.grad(x_space, *coef)
    var = np.einsum("kji, il, ljk -> k", f_prime.T, pcov, f_prime)
    residuals = np.array(y) - curve.equation(np.array(x), *coef)
    rss = np.sum(residuals**2)
    df = len(x) - len(coef)
    crit = stats.t.cdf(1 - alpha / 2, df)
    conf_band = np.sqrt(var) * np.sqrt(rss / df) * crit
    pred_band = np.sqrt(var + 1) * np.sqrt(rss / df) * crit
    return conf_band, pred_band


def graph(curve, y, x, coef, p_cov_list, fit_name, alpha=0.05, n=10000):
    """A method to plot all 16 vial optimal fit and data with intervals.

    A method to plot: 1: the raw data recorded by the evolver, 2: the optimal
    curve fit found by a curve-fitting function, 3: 2-sigma prediction invervals
    that defines expected location of future points, and 4: the 2-sigma fit
    confidence intervals that define where a true optimal fit should lie

    Args:
      curve: an instance of the fit object used
      y: an array-like containing the measured y-values (assumed to be noiseless)
        to predict with the independent variable (x)
      x: an array-like containing the independent variable used for predicting y
      coef: the optimal fit coefficients found from curve fitting
      err: an array containing the covariance matrices of the optimal parameters
      fit_name: A string denoting the fit name used for selecting this fit
      alpha: alpha for prediction and confidence bands, defaults to 0.05
      N: Number of points to plot for best fit line. Defauults to 1000
    """
    func = curve.equation
    model_name = type(curve).__name__
    equation = curve.latex_string

    fig, axes = plt.subplots(4, 4)

    for i in range(16):
        # Obtain fit graph
        xlim = (min(x[i]), max(x[i]))
        ylim = (min(y[i]), max(y[i]))
        x_range = xlim[1] - xlim[0]
        y_range = ylim[1] - ylim[0]
        x_space = np.linspace(xlim[0] - 0.2 * x_range, xlim[1] + 0.2 * x_range, n)
        y_space = func(x_space, *coef[i])
        conf_band, pred_band = calculate_bands(
            curve, y[i], x[i], x_space, coef[i], p_cov_list[i], alpha
        )

        axes[i // 4, (i % 4)].plot(
            x[i], y[i], "o", markersize=3, color="black", label="Raw Data"
        )
        axes[i // 4, (i % 4)].plot(
            x_space, y_space, markersize=1.5, linestyle="-", label="Optimal Fit"
        )
        # Confidence Band
        axes[i // 4, (i % 4)].plot(
            x_space,
            y_space - conf_band,
            markersize=1.5,
            linestyle="--",
            color="green",
            alpha=0.5,
            label=r"95\% Confidence Band",
        )
        axes[i // 4, (i % 4)].plot(
            x_space,
            y_space + conf_band,
            markersize=1.5,
            linestyle="--",
            color="green",
            alpha=0.5,
            label=None,
        )

        # Prediction Band
        axes[i // 4, (i % 4)].plot(
            x_space,
            y_space - pred_band,
            markersize=1.5,
            linestyle="--",
            color="red",
            alpha=0.3,
            label=r"95\% Prediction Band",
        )
        axes[i // 4, (i % 4)].plot(
            x_space,
            y_space + pred_band,
            markersize=1.5,
            linestyle="--",
            color="red",
            alpha=0.3,
            label=None,
        )

        axes[i // 4, (i % 4)].set_xlim(
            [xlim[0] - 0.1 * x_range, xlim[1] + 0.1 * x_range]
        )
        axes[i // 4, (i % 4)].set_ylim(
            [ylim[0] - 0.1 * y_range, ylim[1] + 0.1 * y_range]
        )

        axes[i // 4, (i % 4)].set_title("Vial: " + str(i))
        axes[i // 4, (i % 4)].ticklabel_format(style="sci", axis="y", scilimits=(0, 0))

    # Title and legends before showing and saving plot
    fig.suptitle(f"{model_name} fit using ${equation}$ : {fit_name}")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=4,
        edgecolor="1",
    )
    plt.subplots_adjust(hspace=0.4, wspace=0.2)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    plt.show(block=False)
    return fig


def process_vial_data(calibration, sensors=None):
    """Helper function to process evolver calibration json file

    Data is structed as a list of lists. Each element in the outer list is a vial.
    That element is also a list, one for each point to be fit. The list contains 1
    or more points.  This function takes the median of those points and calculates
    the standard deviation, returning a similar structure.

    refer to formated_calibration.json to see how file is formated
    """
    raw_sets = calibration["raw"]
    if raw_sets is None:
        print("Error processing calibration data - no raw sets found")
        return

    calibration_data = {}
    all_sensor_data = []
    names = []

    # Get raw data from matching sensor list
    for raw_set in raw_sets:
        if sensors is None or raw_set["param"] in sensors:
            all_sensor_data.append(raw_set["vialData"])
            names.append(raw_set.get("param"))

    # Obtain vial sensor reading for each vial, along with some statistics
    measured_standards = calibration["measuredData"]

    for i, sensor_data in enumerate(all_sensor_data):
        means = []
        medians = []
        sensor_readings = []  # x value
        standards = []  # y value

        for vial_num, vial in enumerate(sensor_data):
            standard_measurement = measured_standards[vial_num]

            readings_means = []
            readings_medians = []
            readings_vals = []
            standard_vals = []
            print(f"vial {vial_num:>2}")
            print("  sensor median | standard measurement")
            for idx, readings in enumerate(vial):
                # Flatten sensor readings
                readings_vals += readings
                standard_vals += [standard_measurement[idx]] * len(readings)

                # Obtain statistics
                mean = np.mean(readings)
                median = np.median(readings)

                print(f"{median:>15.0f} | {standard_measurement[idx]:.2f}")
                readings_means.append(mean)
                readings_medians.append(median)
            sensor_readings.append(readings_vals)
            standards.append(standard_vals)
            means.append(readings_means)
            medians.append(readings_medians)
            differences = np.mean(np.array(readings_means) - np.array(readings_medians))
            print(f"average mean - median: {differences:.2f}\n")

        calibration_data[names[i]] = {
            "vial_num": len(sensor_data),
            "means": means,
            "medians": medians,
            "sensor_readings": sensor_readings,
            "standards_data": standards,
        }

    return calibration_data


def log_calibration(raw, path, filename, sensors):
    """Method to write computed curve fit parameters and raw data to file"""
    with open(Path(path, filename), "w", encoding="utf8") as f:
        f.write(f"Calibration Data for {filename[:-4]}\n")

        # Write equation
        eq = raw["eq"]
        f.write(f"Fitted data to {eq} using {sensors} readings\n\n")

        # Write Fit and Raw Data
        records = zip(range(len(raw["coef"])), raw["coef"], raw["err"])

        for vial_num, coef, err in records:
            f.write("=" * 80 + "\n")
            f.write(f"\nCalibration for vial {vial_num:<2}\n")
            for i in range(len(coef)):
                f.write((f"  {string.ascii_lowercase[i]}: {coef[i]} +\\- {err[i]}\n"))

            for sensor in sensors:
                f.write(f"\nRaw Data for {sensor}:\n")
                f.write("  sensor median | standard measurement\n")
                data = raw["data"][sensor]
                # Write Raw Data
                medians = data["medians"][vial_num]
                means = data["means"][vial_num]
                standards_data = data["standards_data"][vial_num]
                raw_data = zip(medians, standards_data)
                diff = 0

                for median, standard_measurement in raw_data:
                    f.write(f"{median:>15.0f} | {standard_measurement:.2f}\n")
                diff = np.mean(np.array(means) - np.array(medians))
                f.write(f"  average mean - median: {diff:.2f}\n\n")
