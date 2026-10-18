#
# horus-runtime
# Copyright (C) 2026 Temple Compute
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
Limit checks shared by the value artifacts: a bounded range, a bounded
count (characters, picks, items) and excluded values. Each raises a
translated ``ValueError`` naming the artifact, so a model validator only
calls them.
"""

from collections.abc import Collection

from horus_runtime.i18n import tr as _

type Number = int | float


def check_range(
    artifact_id: str, value: Number, lo: Number | None, hi: Number | None
) -> None:
    """Raise unless ``lo <= value <= hi``; ``None`` leaves a side open."""
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(
            _("Artifact '%(id)s': min %(min)s is above max %(max)s.")
            % {"id": artifact_id, "min": lo, "max": hi}
        )
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ValueError(
            _("Artifact '%(id)s': %(v)s is outside [%(min)s, %(max)s].")
            % {
                "id": artifact_id,
                "v": value,
                "min": "-inf" if lo is None else lo,
                "max": "inf" if hi is None else hi,
            }
        )


def check_count(
    artifact_id: str, count: int, lo: int | None, hi: int | None, what: str
) -> None:
    """
    Raise unless ``lo <= count <= hi``. *what* names the counted thing
    ("characters", "picks"), already translated.
    """
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(
            _(
                "Artifact '%(id)s': at least %(lo)s %(what)s is more than "
                "the at most %(hi)s."
            )
            % {"id": artifact_id, "lo": lo, "hi": hi, "what": what}
        )
    if lo is not None and count < lo:
        raise ValueError(
            _("Artifact '%(id)s' needs at least %(n)s %(what)s, has %(c)s.")
            % {"id": artifact_id, "n": lo, "what": what, "c": count}
        )
    if hi is not None and count > hi:
        raise ValueError(
            _("Artifact '%(id)s' allows at most %(n)s %(what)s, has %(c)s.")
            % {"id": artifact_id, "n": hi, "what": what, "c": count}
        )


def check_excluded(
    artifact_id: str, value: object, exclude: Collection[object]
) -> None:
    """Raise when *value* is one of the *exclude* values."""
    if value in exclude:
        raise ValueError(
            _("Artifact '%(id)s': '%(v)s' is not allowed.")
            % {"id": artifact_id, "v": value}
        )
