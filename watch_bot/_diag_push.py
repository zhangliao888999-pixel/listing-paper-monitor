import subprocess
import time

for i in range(20):
    r = subprocess.run(["git", "push"], cwd=".", capture_output=True, text=True, timeout=60)
    print(f"--- attempt {i} ---")
    print("RC:", r.returncode)
    if r.returncode != 0:
        print("STDOUT:", repr(r.stdout))
        print("STDERR:", repr(r.stderr))
        pull = subprocess.run(["git", "pull", "--no-edit", "origin", "master"], cwd=".",
                               capture_output=True, text=True, timeout=60)
        print("PULL RC:", pull.returncode)
        print("PULL STDOUT:", repr(pull.stdout))
        print("PULL STDERR:", repr(pull.stderr))
    else:
        print("push succeeded, nothing more to test, stopping")
        break
    time.sleep(3)
