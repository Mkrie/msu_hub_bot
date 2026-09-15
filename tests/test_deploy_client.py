import importlib.util
from pathlib import Path
from subprocess import CompletedProcess

SPEC = importlib.util.spec_from_file_location("deploy_client", Path(__file__).resolve().parents[1] / "tools/deploy_client.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def test_public_deployment_output_excludes_ssh_diagnostics(capsys):
    result = CompletedProcess(
        args=[],
        returncode=1,
        stdout="host banner at 192.0.2.42\nImage pulled; checking configuration and connections\n",
        stderr="Permission denied for operator@192.0.2.42 using /private/example/key",
    )
    client.report_result(result)
    output = capsys.readouterr()
    assert "Image pulled" in output.out
    assert "Deployment failed" in output.err
    assert "192.0.2.42" not in output.out + output.err
    assert "/private/example/key" not in output.out + output.err
