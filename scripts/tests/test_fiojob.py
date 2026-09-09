import os
import tempfile
import unittest

from lib.fiojob import parse_job_file, parse_size


class ParseSizeTest(unittest.TestCase):
    def test_binary_suffixes(self):
        self.assertEqual(parse_size("10G"), 10 * 1024 ** 3)
        self.assertEqual(parse_size("512k"), 512 * 1024)
        self.assertEqual(parse_size("1m"), 1024 ** 2)
        self.assertEqual(parse_size("4096"), 4096)


class ParseJobFileTest(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".fio")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_global_numjobs_multiplies_size(self):
        path = self._write(
            "[global]\nsize=10G\nnumjobs=8\nrw=randrw\nrwmixread=50\nbs=4k\n\n"
            "[job1]\nrate_iops=188,187\n"
        )
        spec = parse_job_file(path)
        self.assertEqual(spec.numjobs, 8)
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)
        self.assertEqual(spec.directions, {"read", "write"})

    def test_eight_sections_without_numjobs_sum_to_eight_clones(self):
        body = "[global]\nsize=10G\n\n"
        for i in range(4):
            body += "[w%d]\nrw=randwrite\nbs=32k\nrate_iops=375\n\n" % i
            body += "[r%d]\nrw=randread\nbs=32k\nrate_iops=375\n\n" % i
        spec = parse_job_file(self._write(body))
        self.assertEqual(spec.numjobs, 8)
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)
        self.assertEqual(spec.directions, {"read", "write"})

    def test_write_only_direction(self):
        spec = parse_job_file(
            self._write("[global]\nsize=1G\nnumjobs=2\n\n[j]\nrw=randwrite\nbs=4k\n")
        )
        self.assertEqual(spec.directions, {"write"})

    def test_option_falls_back_from_global_to_section(self):
        """bs and rw live in the job section in most of these files, not in
        [global]. Metadata generation must not report them as absent."""
        spec = parse_job_file(self._write(
            "[global]\nsize=10G\nnumjobs=8\niodepth=32\n\n"
            "[job1]\nrw=randwrite\nbs=32k\n"))
        self.assertEqual(spec.option("bs"), "32k")
        self.assertEqual(spec.option("rw"), "randwrite")
        self.assertEqual(spec.option("iodepth"), "32")
        self.assertIsNone(spec.option("nonesuch"))

    def test_global_wins_over_section_for_the_same_option(self):
        spec = parse_job_file(self._write(
            "[global]\nsize=1G\nbs=4k\n\n[j]\nrw=read\nbs=64k\n"))
        self.assertEqual(spec.option("bs"), "4k")

    def test_inline_comment_is_stripped(self):
        spec = parse_job_file(
            self._write("[global]\nsize=10G   ; per job\nnumjobs=8\n\n[j]\nrw=read\n")
        )
        self.assertEqual(spec.required_bytes, 8 * 10 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
