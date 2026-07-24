# -*- coding: utf-8 -*-
"""cli Plan 3 子命令注册测试（subprocess --help，非 slow）。"""
import subprocess
import sys


def test_run_help_has_tpe_and_rho_args():
    """run 子命令注册了 --tpe-trials / --rho-threshold（Plan 3 新参数）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "run", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "--tpe-trials" in p.stdout
    assert "--rho-threshold" in p.stdout


def test_champions_subcommand_registered():
    """champions 子命令注册（Plan 3 新增）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "champions", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "--top-n" in p.stdout


def test_report_subcommand_registered():
    """report 子命令注册（Plan 3 新增）。"""
    p = subprocess.run([sys.executable, "-m", "discovery", "report", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0
