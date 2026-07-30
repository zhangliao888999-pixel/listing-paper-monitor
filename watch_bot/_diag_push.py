import subprocess
r = subprocess.run(["git", "push"], cwd=".", capture_output=True, text=True, timeout=60)
print("RC:", r.returncode)
print("STDOUT:", repr(r.stdout))
print("STDERR:", repr(r.stderr))
