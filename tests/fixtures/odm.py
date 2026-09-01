"""
An in-memory stand-in for the Beanie documents.

The suite runs without MongoDB, and the detection modules are pure functions
so they never needed one. The services are the layer that does: they read the
whole history, filter assignments by trip, and write a decision back. Standing
each document up with ``SimpleNamespace`` works until a service builds a query
— ``TripAssignment.trip_id == trip_id`` is an expression on the real document,
and a plain class attribute would evaluate it to ``True`` or ``False`` and
filter nothing.

So the field names here are markers rather than values. Comparing one produces
a predicate, ``find`` applies every predicate it was given, and an instance
attribute of the same name shadows the marker — which is what lets a document
carry an ordinary value while its class still describes a query.

This is deliberately the smallest thing that answers the queries the services
actually make. It is not a MongoDB.
"""

import operator
from itertools import count


class Predicate:
    """One ``field <op> value`` test, as a callable over a document."""

    def __init__(self, field: str, test, value) -> None:
        self.field = field
        self.test = test
        self.value = value

    def __call__(self, document) -> bool:
        return self.test(getattr(document, self.field, None), self.value)


class FieldRef:
    """
    A field named on the class, so a comparison becomes a query.

    Equality is the common case, and the ordering operators are here because
    the drill-down asks for a half-open date range rather than a single month
    value.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other) -> Predicate:
        return Predicate(self.name, operator.eq, other)

    def __ne__(self, other) -> Predicate:
        return Predicate(self.name, operator.ne, other)

    def __lt__(self, other) -> Predicate:
        return Predicate(self.name, operator.lt, other)

    def __le__(self, other) -> Predicate:
        return Predicate(self.name, operator.le, other)

    def __gt__(self, other) -> Predicate:
        return Predicate(self.name, operator.gt, other)

    def __ge__(self, other) -> Predicate:
        return Predicate(self.name, operator.ge, other)

    def __hash__(self) -> int:
        return hash(self.name)


def _as_test(predicate):
    """
    One filter as a callable.

    Most queries here are built from the field markers above, but the PayPal
    store asks for a whole file's worth of codes at once and writes that as a
    raw ``{"code": {"$in": [...]}}`` document, so that form is understood too.
    """
    if not isinstance(predicate, dict):
        return predicate

    def matches(document) -> bool:
        for field, condition in predicate.items():
            value = getattr(document, field, None)
            if isinstance(condition, dict):
                if "$in" in condition and value not in condition["$in"]:
                    return False
            elif value != condition:
                return False
        return True

    return matches


class Query:
    """The ``find(...).sort(...).to_list()`` chain, over a list."""

    def __init__(self, rows: list) -> None:
        self.rows = rows

    def sort(self, *keys):
        for key in reversed(keys):
            descending = key.startswith("-")
            name = key.lstrip("-+")
            self.rows.sort(key=lambda row: getattr(row, name), reverse=descending)
        return self

    def skip(self, count: int):
        self.rows = self.rows[count:]
        return self

    def limit(self, count: int):
        self.rows = self.rows[:count]
        return self

    async def to_list(self) -> list:
        return list(self.rows)

    async def first_or_none(self):
        return self.rows[0] if self.rows else None

    async def count(self) -> int:
        return len(self.rows)


class FakeDocument:
    """
    Base for a stood-in document.

    ``rows`` is the collection and lives on the subclass, so each test gets a
    fresh one.

    Ids are 24-character hex strings rather than anything more readable,
    because the services parse them: ``TripService._get`` builds a
    ``PydanticObjectId`` and treats a failure to parse as "no such trip", so a
    friendlier id would make every lookup here miss for the wrong reason.
    """

    rows: list = []
    _ids = count(1)

    def __init__(self, **fields) -> None:
        for name, value in fields.items():
            setattr(self, name, value)
        # Tested against the instance dictionary rather than with hasattr:
        # a class declaring "id" as a queryable field always has the marker,
        # so hasattr is true before the document has an id of its own.
        if "id" not in self.__dict__:
            self.id = f"{next(FakeDocument._ids):024x}"

    # --- Reading ------------------------------------------------------------

    @classmethod
    def find(cls, *predicates) -> Query:
        tests = [_as_test(predicate) for predicate in predicates]
        return Query([row for row in cls.rows if all(test(row) for test in tests)])

    @classmethod
    def find_all(cls) -> Query:
        return Query(list(cls.rows))

    @classmethod
    async def find_one(cls, *predicates):
        for row in cls.rows:
            if all(p(row) for p in predicates):
                return row
        return None

    @classmethod
    async def get(cls, document_id):
        wanted = str(document_id)
        return next((row for row in cls.rows if str(row.id) == wanted), None)

    # --- Writing ------------------------------------------------------------

    async def insert(self):
        type(self).rows.append(self)
        return self

    @classmethod
    async def insert_many(cls, documents):
        cls.rows.extend(documents)
        return documents

    async def save(self):
        if self not in type(self).rows:
            type(self).rows.append(self)
        return self

    async def delete(self):
        rows = type(self).rows
        if self in rows:
            rows.remove(self)


def document(name: str, *fields: str) -> type:
    """
    A stand-in document class with the given queryable fields.

    Args:
        name: Class name, used for readable ids and failure messages.
        fields: Every field a service compares against in a query. A field
            only read off an instance needs no marker here.
    """
    namespace = {field: FieldRef(field) for field in fields}
    namespace["rows"] = []
    return type(name, (FakeDocument,), namespace)
