import unittest

from nfsstat import delta, parse_mountstats

MOUNT = "/opt/nifi/nifi-current/content_repository"

SAMPLE = """device 10.0.0.5:/export mounted on %s with fstype nfs statvers=1.1
\topts:\trw,vers=4.1,rsize=1048576,wsize=1048576,hard,proto=tcp
\tage:\t3600
\tper-op statistics
\t        READ: 1000 1000 0 128000 1024000 500 12000 13000
\t       WRITE: 2000 2000 3 256000 2048000 900 48000 51000
\t      COMMIT: 50 50 0 3200 4000 10 900 950
""" % MOUNT

NOT_NFS = """device /dev/sda1 mounted on /var with fstype ext4
\topts:\trw
"""


class ParseMountstatsTest(unittest.TestCase):
    def test_extracts_version_and_per_op_counters(self):
        m = parse_mountstats(SAMPLE)
        mount = m[MOUNT]
        self.assertEqual(mount["vers"], "4.1")
        self.assertEqual(mount["ops"]["WRITE"]["ops"], 2000)
        self.assertEqual(mount["ops"]["WRITE"]["timeouts"], 3)
        self.assertEqual(mount["ops"]["WRITE"]["rtt_ms"], 48000)
        self.assertEqual(mount["ops"]["READ"]["bytes_recv"], 1024000)

    def test_skips_non_nfs_filesystems(self):
        self.assertEqual(parse_mountstats(NOT_NFS), {})

    def test_multiple_mounts(self):
        two = SAMPLE + SAMPLE.replace(MOUNT, "/opt/nifi/x/provenance_repository")
        m = parse_mountstats(two)
        self.assertEqual(len(m), 2)


class DeltaTest(unittest.TestCase):
    def _after(self):
        return parse_mountstats(SAMPLE.replace(
            "WRITE: 2000 2000 3 256000 2048000 900 48000 51000",
            "WRITE: 3000 3200 5 384000 3072000 1400 78000 82000"))

    def test_mean_rtt_over_the_window(self):
        """30ms mean write RTT over the 1000 operations in this window --
        not over the whole life of the mount."""
        d = delta(parse_mountstats(SAMPLE), self._after(), MOUNT)
        self.assertEqual(d["WRITE"]["ops"], 1000)
        self.assertEqual(d["WRITE"]["timeouts"], 2)
        self.assertAlmostEqual(d["WRITE"]["mean_rtt_ms"], 30.0)
        self.assertAlmostEqual(d["WRITE"]["mean_queue_ms"], 0.5)

    def test_retransmits(self):
        """ntrans above ops means the client resent RPCs. That is the one
        fio-side symptom that genuinely supports a 'network retry' claim."""
        d = delta(parse_mountstats(SAMPLE), self._after(), MOUNT)
        self.assertAlmostEqual(d["WRITE"]["retrans_pct"], 20.0)

    def test_idle_op_has_no_mean_rather_than_zero(self):
        """Zero operations must not report 0.00 ms, which reads as instant."""
        d = delta(parse_mountstats(SAMPLE), parse_mountstats(SAMPLE), MOUNT)
        self.assertEqual(d["WRITE"]["ops"], 0)
        self.assertIsNone(d["WRITE"]["mean_rtt_ms"])
        self.assertIsNone(d["WRITE"]["retrans_pct"])

    def test_unknown_mount_is_empty(self):
        self.assertEqual(delta(parse_mountstats(SAMPLE),
                               parse_mountstats(SAMPLE), "/nope"), {})


if __name__ == "__main__":
    unittest.main()
