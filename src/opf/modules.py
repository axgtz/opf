from functools import reduce
from math import exp
from typing import List, Dict

import alegnn.utils.graphML as gml
import pytorch_lightning as pl
import torch
import torch.nn
from alegnn.modules import architectures

import opf.powerflow as pf
from opf.power import NetWrapper
from opf.constraints import equality, inequality


class GNN(pl.LightningModule):
    def __init__(self, gso, features, taps, mlp):
        super().__init__()
        self.save_hyperparameters(ignore=["gso"])

        n_layers = len(taps)
        self.gnn = architectures.SelectionGNN(
            features,
            taps,
            True,
            torch.nn.Tanh,
            [gso.shape[-1]] * n_layers,
            gml.NoPool,
            [1] * n_layers,
            mlp,
            gso,
        )

    def forward(self, x):
        return self.gnn(x)

class LocalGNN(pl.LightningModule):
    def __init__(self, gso, features, taps, output_features):
        super().__init__()
        self.save_hyperparameters(ignore=["gso"])

        n_layers = len(taps)
        self.gnn = architectures.LocalGNN(
            features,
            taps,
            True,
            torch.nn.Tanh,
            [gso.shape[-1]] * n_layers,
            gml.NoPool,
            [1] * n_layers,
            output_features,
            gso,
        )
    
    def forward(self, x):
        return self.gnn(x)

class MultiReadout(torch.nn.Module):
    def __init__(self, nodes: int, F_in: int, F_out: int, use_bias: bool = True):
        super().__init__()
        self.use_bias = use_bias
        self.input_dims = (-1, nodes, F_in)

        self.weight = torch.nn.Parameter(torch.zeros(nodes, F_out, F_in))
        torch.nn.init.xavier_normal_(self.weight)
        if self.use_bias:
            self.bias = torch.nn.Parameter(torch.zeros(nodes, F_out))

    def forward(self, x):
        x = x.reshape(self.input_dims)
        return torch.einsum("njk,bnk -> bnj", self.weight, x) + self.bias


class OPFLogBarrier(pl.LightningModule):
    def __init__(
        self,
        net_wrapper: NetWrapper,
        model,
        t=10,
        s=1000,
        cost_weight=1.0,
        lr=1e-4,
        eps=1e-3,
        constraint_features=False,
        enforce_constraints=False,
        **kwargs,
    ):
        super().__init__()
        self.net_wrapper = net_wrapper
        self.pm = self.net_wrapper.to_powermodels()
        self.model = model
        self.detailed_metrics = False
        self.save_hyperparameters(ignore=["net_wrapper", "model", "kwargs"])

        # Parse parameters such as admittance matrix to be used in powerflow calculations.
        self.powerflow_parameters = pf.parameters_from_pm(self.pm)

        # Normalization factor to be applied to the cost function
        self.cost_normalization = 1.0

    def forward(self, load):
        if self.hparams.constraint_features:
            x = torch.cat(
                (
                    load,
                    self.bus_constraints_matrix.T.unsqueeze(0).repeat(
                        load.shape[0], 1, 1
                    ),
                ),
                dim=1,
            )
        else:
            x = load
        bus = self.model(x)
        bus = torch.reshape(bus, (-1, 4, self.powerflow_parameters.n_bus))
        V, S = self.parse_bus(bus)
        Sd = self.parse_load(load)
        if self.hparams.enforce_constraints:
            V, S = self.enforce_constraints(V, S, Sd)
        return V, S, Sd

    def sigmoid_bound(self, x, lb, ub):
        scale = ub - lb
        return scale * torch.sigmoid(x) + lb

    def enforce_constraints(self, V, S, Sd):
        Sg = S + Sd
        vm_constraint = self.powerflow_parameters.constraints[
            "inequality/voltage_magnitude"
        ]
        active_constraint = self.powerflow_parameters.constraints[
            "inequality/active_power"
        ]
        reactive_constraint = self.powerflow_parameters.constraints[
            "inequality/reactive_power"
        ]
        vm = self.sigmoid_bound(V.abs(), vm_constraint.min, vm_constraint.max)
        V = torch.polar(vm, V.angle()) # V * vm / V.abs()

        Sg.real = self.sigmoid_bound(
            Sg.real, active_constraint.min, active_constraint.max
        )
        Sg.imag = self.sigmoid_bound(
            Sg.imag, reactive_constraint.min, reactive_constraint.max
        )
        S = Sg - Sd
        return V, S

    def _step_helper(self, V, S, Sd, project_pandapower=False):
        if project_pandapower:
            V, S = self.project_pandapower(V, S, Sd)
        variables = pf.powerflow(V, S, Sd, self.powerflow_parameters)
        constraints = self.constraints(variables)
        cost = self.cost(variables)
        loss = self.loss(cost, constraints)
        return variables, constraints, cost, loss

    def training_step(self, batch, *args):
        load = batch[0] @ self.powerflow_parameters.load_matrix
        _, constraints, cost, loss = self._step_helper(
            *self(load), project_pandapower=False
        )
        self.log("train/loss", loss, prog_bar=True, sync_dist=True)
        self.log_dict(
            self.metrics(cost, constraints, "train", self.detailed_metrics),
            sync_dist=True,
        )
        return loss

    def validation_step(self, batch, *args):
        with torch.no_grad():
            load = batch[0] @ self.powerflow_parameters.load_matrix
            _, constraints, cost, loss = self._step_helper(
                *self(load), project_pandapower=False
            )
            self.log("val/loss", loss, prog_bar=True, sync_dist=True)
            self.log_dict(
                self.metrics(cost, constraints, "val", self.detailed_metrics),
                sync_dist=True,
            )

    def test_step(self, batch, *args):
        with torch.no_grad():
            load, acopf_bus = batch
            load @= self.powerflow_parameters.load_matrix
            _, constraints, cost, _ = self._step_helper(
                *self(load), project_pandapower=True
            )
            test_metrics = self.metrics(
                cost, constraints, "test", self.detailed_metrics
            )
            self.log_dict(test_metrics)

            # Test the ACOPF solution for reference.
            acopf_bus = self.bus_from_polar(acopf_bus)
            _, constraints, cost, _ = self._step_helper(
                *self.parse_bus(acopf_bus),
                self.parse_load(load),
                project_pandapower=False,
            )
            acopf_metrics = self.metrics(
                cost, constraints, "acopf", self.detailed_metrics
            )
            self.log_dict(acopf_metrics)
            return dict(**test_metrics, **acopf_metrics)

    def parse_bus(self, bus: torch.Tensor):
        assert bus.shape[1] == 4
        assert bus.shape[2] == self.powerflow_parameters.n_bus

        # Convert voltage and power to per unit
        vr = bus[:, 0, :]
        vi = bus[:, 1, :]
        p = bus[:, 2, :]
        q = bus[:, 3, :]

        V = torch.complex(vr, vi).unsqueeze(-1)
        S = torch.complex(p, q).unsqueeze(-1)
        return V, S

    def parse_load(self, load: torch.Tensor):
        assert load.shape[1] == 2
        assert load.shape[2] == self.powerflow_parameters.n_bus
        Sd = torch.complex(load[:, 0, :], load[:, 1, :]).unsqueeze(-1)
        return Sd

    def loss(self, cost, constraints):
        constraint_losses = [
            val["loss"]
            for val in constraints.values()
            if val["loss"] is not None and not torch.isnan(val["loss"])
        ]
        if len(constraint_losses) == 0:
            constraint_losses = [torch.zeros(1, device=self.device, dtype=self.dtype)]
        return (
            cost * self.hparams.cost_weight * self.cost_normalization
            + torch.stack(constraint_losses).sum()
        )

    def cost(self, variables: pf.PowerflowVariables) -> torch.Tensor:
        """Compute the cost to produce the active and reactive power."""
        p = variables.Sg.real
        p_coeff = self.powerflow_parameters.cost_coeff
        return (p_coeff[:, 0] + p * p_coeff[:, 1] + (p ** 2) * p_coeff[:, 2]).mean()

    def constraints(self, variables) -> Dict[str, Dict[str, torch.Tensor]]:
        """
        Calculates the powerflow constraints.
        :returns: Nested map from constraint name => (value name => tensor value)
        """
        values = {}
        for name, constraint in self.powerflow_parameters.constraints.items():
            if isinstance(constraint, pf.EqualityConstraint):
                values[name] = equality(
                    constraint.value(self.powerflow_parameters, variables),
                    constraint.target(self.powerflow_parameters, variables),
                    self.hparams.eps,
                    constraint.isAngle,
                )
            elif isinstance(constraint, pf.InequalityConstraint):
                values[name] = inequality(
                    constraint.variable(self.powerflow_parameters, variables),
                    constraint.min,
                    constraint.max,
                    self.hparams.s,
                    self.hparams.t,
                    self.hparams.eps,
                    constraint.isAngle,
                )
        return values

    @property
    def bus_constraints_matrix(self):
        """Returns a matrix representing the bus constraints as a graph signal."""
        bus_constraints = []
        for constraint in self.powerflow_parameters.constraints.values():
            if constraint.isBus:
                bus_constraints += [constraint.min, constraint.max]
        return torch.cat(bus_constraints, dim=1).to(self.device)

    @property
    def branch_constraints_matrix(self):
        """Returns a matrix representing the branch constraint features.
        The matrix size is # branches x # branch constraints"""
        branch_constraints = []
        for constraint in self.powerflow_parameters.constraints.values():
            if constraint.isBranch:
                branch_constraints += [constraint.min, constraint.max]
        return torch.stack(branch_constraints, dim=0)

    def metrics(self, cost, constraints, prefix, detailed=False):
        aggregate_metrics = {
            f"{prefix}/cost": [cost],
            f"{prefix}/equality/loss": [],
            f"{prefix}/equality/rate": [],
            f"{prefix}/equality/error_mean": [],
            f"{prefix}/equality/error_max": [],
            f"{prefix}/inequality/loss": [],
            f"{prefix}/inequality/rate": [],
            f"{prefix}/inequality/error_mean": [],
            f"{prefix}/inequality/error_max": [],
        }
        detailed_metrics = {}
        reduce_fn = {
            "default": torch.sum,
            "error_mean": torch.mean,
            "error_max": torch.max,
            "rate": torch.mean,
        }

        for constraint_name, constraint_values in constraints.items():
            constraint_type = constraint_name.split("/")[0]
            for value_name, value in constraint_values.items():
                if detailed:
                    detailed_metrics[f"{prefix}/{constraint_name}/{value_name}"] = value
                aggregate_name = f"{prefix}/{constraint_type}/{value_name}"
                aggregate_metrics[aggregate_name].append(value.reshape(1))
        for aggregate_name in aggregate_metrics:
            value_name = aggregate_name.rsplit("/", 1)[1]
            fn = (
                reduce_fn[value_name]
                if value_name in reduce_fn
                else reduce_fn["default"]
            )
            aggregate_metrics[aggregate_name] = fn(
                torch.stack(aggregate_metrics[aggregate_name])
            )
        return {**aggregate_metrics, **detailed_metrics}

    def project_pandapower(self, V: torch.Tensor, S: torch.Tensor, Sd: torch.Tensor):
        with torch.no_grad():
            Sg = S + Sd
            self.net_wrapper.set_gen_sparse(
                Sg.real.squeeze().cpu().numpy(), Sg.imag.squeeze().cpu().numpy()
            )
            self.net_wrapper.set_load_sparse(
                Sd.real.squeeze().cpu().numpy(), Sd.imag.squeeze().cpu().numpy()
            )
            res_powerflow = self.net_wrapper.powerflow()
            if res_powerflow is None:
                return V, S
            else:
                bus, _, _ = res_powerflow
                bus = torch.as_tensor(bus, device=self.device, dtype=self.dtype)
                bus = self.bus_from_polar(bus.unsqueeze(0))
                return self.parse_bus(bus)

    def configure_optimizers(self):
        return torch.optim.AdamW(self.parameters(), self.hparams.lr)

    @staticmethod
    def bus_from_polar(bus):
        """
        Convert bus voltage from polar to rectangular.
        """
        bus = bus.clone()
        V = torch.polar(bus[:, 0, :], bus[:, 1, :])
        bus[:, 0, :] = V.real
        bus[:, 1, :] = V.imag
        return bus

    @staticmethod
    def bus_to_polar(bus):
        """
        Convert bus voltage from rectangular to polar.
        """
        bus = bus.clone()
        V = torch.complex(bus[:, 0, :], bus[:, 1, :])
        bus[:, 0, :] = V.abs()
        bus[:, 1, :] = V.angle()
        return bus
