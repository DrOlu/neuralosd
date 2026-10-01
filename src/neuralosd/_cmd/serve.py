import os, sys
def run(a):
    d = os.path.abspath(a.instance_dir)
    os.chdir(d); sys.path.insert(0, d)
    import serve; serve.main()
