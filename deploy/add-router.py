"""add-router.py <dynamic.yml> <name> <address> <host>...: a TCP passthrough router on the
console entrypoint, inserted before the `adapter` catch-all, and its service."""
import sys
p, name, addr, hosts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
s = open(p).read()
assert ("    %s:\n" % name) not in s, "router exists"
rule = " || ".join("HostSNI(`%s`)" % h for h in hosts)
router = ("    %s:\n      # Split from nx-baas (nx-baas docs/service-split.md).\n      entryPoints: [console]\n"
          "      rule: \"%s\"\n      priority: 100\n      service: %s\n      tls:\n        passthrough: true\n") % (name, rule, name)
anchor = "    adapter:\n      entryPoints: [console]\n"
assert s.count(anchor) == 1
s = s.replace(anchor, router + anchor)
svc = "    nx-baas:\n      loadBalancer:\n        servers:\n          - address: \"openpak-nx-baas:21000\"\n"
assert s.count(svc) == 1
s = s.replace(svc, svc + "    %s:\n      loadBalancer:\n        servers:\n          - address: \"%s\"\n" % (name, addr))
open(p, "w").write(s)
print("router", name, "added")
