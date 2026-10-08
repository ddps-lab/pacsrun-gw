"""The machine and the GPU as two values (HYPERUN-INSTANCE-AND-GPU).

Every instance type below is one a real job ran on, read from the HyperunJobs on the
cluster on 2026-10-08. Before this change the API's `gpu` field held that one vendor
name for every vendor, so a CPU job's console page read "GPU  t3.xlarge".
"""
import pytest

from ddpsrun_server import machine, models

JobView = models.JobView


def _job(vendor=None, instance_type=None, gpus=None, phase="Running"):
    spec = {"resources": {"cpus": "2", "memory": "4Gi"}}
    if gpus is not None:
        spec["resources"]["gpus"] = gpus
    status = {"phase": phase}
    if vendor:
        status["currentOffering"] = {"vendor": vendor, "instanceType": instance_type,
                                     "zone": "z", "capacityType": "spot"}
    return {"metadata": {"name": "hyperun-a8acdef80a07", "namespace": "lab-alice",
                         "labels": {}},
            "spec": spec, "status": status}


@pytest.mark.parametrize("vendor, instance_type, gpus, want", [
    # A CPU job on AWS: the machine, and no GPU line at all.
    ("aws", "t3.xlarge", None, ("t3.xlarge", None, None)),
    ("aws", "c6a.2xlarge", None, ("c6a.2xlarge", None, None)),
    # AWS GPU machines: the model comes from aws_gpus.csv, the count from the ask.
    ("aws", "g6.2xlarge", {"count": 1, "name": "L4"}, ("g6.2xlarge", "L4", 1)),
    ("aws", "gr6.4xlarge", {"count": 1, "name": "L4"}, ("gr6.4xlarge", "L4", 1)),
    # Asked by memory, not by name: the table still knows the card.
    ("aws", "g4dn.12xlarge", {"count": 1, "vramGB": 16}, ("g4dn.12xlarge", "T4", 1)),
    ("aws", "g7.12xlarge", {"count": 2, "vramGB": 24}, ("g7.12xlarge", "RTX PRO 4500", 2)),
    # GCP: machine type before "+", accelerator between "+" and ":".
    ("gcp", "g2-standard-4+L4:1", {"count": 1, "name": "L4"}, ("g2-standard-4", "L4", 1)),
    # Shadeform: the card prices.csv holds for the instance name ...
    ("shadeform", "crusoe_A100-80GBx4", {"count": 4, "name": "A100-80GB"},
     ("crusoe_A100-80GBx4", "A100-80GB", 4)),
    ("shadeform", "massedcompute_L40S", {"count": 1, "name": "L40S"},
     ("massedcompute_L40S", "L40S", 1)),
    # ... and the name with the cloud and the count taken off when it holds none.
    ("shadeform", "massedcompute_A6000", {"count": 1, "vramGB": 16},
     ("massedcompute_A6000", "A6000", 1)),
    # Massed Compute's size variant: "-plus" is part of the machine's name, not the
    # card's. The job named the card, and the leftover extends that name.
    ("shadeform", "massedcompute_A6000-plus", {"count": 1, "name": "A6000"},
     ("massedcompute_A6000-plus", "A6000", 1)),
    # A card the price table knows is never shortened to the name the job asked by.
    ("shadeform", "crusoe_A100-80GBx4", {"count": 4, "name": "A100"},
     ("crusoe_A100-80GBx4", "A100-80GB", 4)),
    # RunPod rents a pod by GPU type: no machine name, the type IS the GPU.
    ("runpod", "NVIDIA A100-SXM4-80GB", {"count": 4, "name": "A100"},
     (None, "A100-SXM4-80GB", 4)),
    ("runpod", "NVIDIA RTX 2000 Ada Generation", {"count": 1, "vramGB": 16},
     (None, "RTX 2000 Ada Generation", 1)),
])
def test_each_vendor_splits_into_machine_and_gpu(vendor, instance_type, gpus, want):
    view = JobView.from_hyperunjob(_job(vendor, instance_type, gpus))
    assert (view.instance, view.gpu_model, view.gpu_count) == want


def test_the_old_gpu_field_keeps_its_old_value():
    # CLI 0.2.7 and older print `running on <gpu>`; an installed CLI cannot be
    # changed from the server, so the field it reads must not move.
    view = JobView.from_hyperunjob(_job("aws", "t3.xlarge"))
    assert view.gpu == "t3.xlarge"
    assert view.vendor == "aws"


def test_before_a_machine_exists_only_the_ask_is_known():
    view = JobView.from_hyperunjob(_job(gpus={"count": 2, "name": "L40S"}, phase=""))
    assert (view.instance, view.gpu_model, view.gpu_count) == (None, None, 2)


def test_a_cpu_job_never_reports_a_gpu_even_on_a_gpu_machine():
    # The console leaves the GPU line out when gpu_count is None, so a job that
    # asked for none must not borrow its machine's card.
    view = JobView.from_hyperunjob(_job("aws", "g6.2xlarge"))
    assert (view.instance, view.gpu_model, view.gpu_count) == ("g6.2xlarge", None, None)


def test_a_compared_job_rented_nothing():
    view = JobView.from_hyperunjob(_job(phase="Compared"))
    assert (view.instance, view.gpu_model, view.gpu_count) == (None, None, None)


def test_an_omitted_count_reads_as_the_crd_default_of_one():
    assert machine.requested_gpus({"resources": {"gpus": {"vramGB": 48}}}) == (1, None)
    assert machine.requested_gpus({"resources": {}}) == (None, None)


def test_the_aws_table_ships_and_leaves_cpu_machines_out():
    table = machine._aws_gpus()
    assert table["g6.2xlarge"] == "L4"
    assert table["g4dn.xlarge"] == "T4"
    assert "t3.xlarge" not in table
