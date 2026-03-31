import os
import shutil
import site

def apply_patch():
    try:
        site_packages = site.getsitepackages()[0]
        target = os.path.join(site_packages, "pyart", "io")
        source = os.path.join(os.getcwd(), "patches", "io")

        if not os.path.exists(source):
            print("⚠️ Patch folder not found")
            return

        print(f"🔧 Patching Py-ART IO...")
        for f in os.listdir(source):
            src_file = os.path.join(source, f)
            dst_file = os.path.join(target, f)
            shutil.copy2(src_file, dst_file)
            print(f"✔ patched: {f}")

    except Exception as e:
        print("❌ Patch failed:", e)

apply_patch()