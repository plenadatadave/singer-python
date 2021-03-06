import datetime
import pendulum
from jsonschema import RefResolver
from singer.logger import get_logger
from singer.utils import strftime

LOGGER = get_logger()

NO_INTEGER_DATETIME_PARSING = "no-integer-datetime-parsing"
UNIX_SECONDS_INTEGER_DATETIME_PARSING = "unix-seconds-integer-datetime-parsing"
UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING = "unix-milliseconds-integer-datetime-parsing"

VALID_DATETIME_FORMATS = [
    NO_INTEGER_DATETIME_PARSING,
    UNIX_SECONDS_INTEGER_DATETIME_PARSING,
    UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
]


def string_to_datetime(value):
    try:
        return strftime(pendulum.parse(value))
    except:
        return None


def unix_milliseconds_to_datetime(value):
    return strftime(datetime.datetime.fromtimestamp(float(value) / 1000.0, datetime.timezone.utc))


def unix_seconds_to_datetime(value):
    return strftime(datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc))


class SchemaMismatch(Exception):
    def __init__(self, errors):
        if not errors:
            msg = "An error occured during transform that was not a schema mismatch"

        else:
            estrs = [e.tostr() for e in errors]
            msg = "Errors during transform\n\t{}".format("\n\t".join(estrs))
            msg += "\n\n\nErrors during transform: [{}]".format(", ".join(estrs))

        super(SchemaMismatch, self).__init__(msg)

class SchemaKey:
    ref = "$ref"
    items = "items"
    properties = "properties"
    pattern_properties = "patternProperties"

class Error:
    def __init__(self, path, data, schema=None):
        self.path = path
        self.data = data
        self.schema = schema

    def tostr(self):
        path = ".".join(map(str, self.path))
        if self.schema:
            msg = "does not match {}".format(self.schema)
        else:
            msg = "not in schema"

        return "{}: {} {}".format(path, self.data, msg)


class Transformer:
    def __init__(self, integer_datetime_fmt=NO_INTEGER_DATETIME_PARSING, pre_hook=None):
        self.integer_datetime_fmt = integer_datetime_fmt
        self.pre_hook = pre_hook
        self.removed = set()
        self.errors = []

    def log_warning(self):
        if self.removed:
            LOGGER.warning("Removed %s paths during transforms:\n\t%s",
                           len(self.removed),
                           "\n\t".join(sorted(self.removed)))
            # Output list format to parse for reporting
            LOGGER.warning("Removed paths list: %s", sorted(self.removed))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.log_warning()

    def transform(self, data, schema):
        success, transformed_data = self.transform_recur(data, schema, [])
        if not success:
            raise SchemaMismatch(self.errors)

        return transformed_data

    def transform_recur(self, data, schema, path):
        if "anyOf" in schema:
            return self._transform_anyof(data, schema, path)

        if "type" not in schema:
            # indicates no typing information so don't bother transforming it
            return True, data

        types = schema["type"]
        if not isinstance(types, list):
            types = [types]

        if "null" in types:
            types.remove("null")
            types.append("null")

        for typ in types:
            success, transformed_data = self._transform(data, typ, schema, path)
            if success:
                return success, transformed_data
        else: # pylint: disable=useless-else-on-loop
            # exhaused all types and didn't return, so we failed :-(
            self.errors.append(Error(path, data, schema))
            return False, None

    def _transform_anyof(self, data, schema, path):
        subschemas = schema['anyOf']
        for subschema in subschemas:
            success, transformed_data = self.transform_recur(data, subschema, path)
            if success:
                return success, transformed_data
        else: # pylint: disable=useless-else-on-loop
            # exhaused all schemas and didn't return, so we failed :-(
            self.errors.append(Error(path, data, schema))
            return False, None

    def _transform_object(self, data, schema, path):
        # We do not necessarily have a dict to transform here. The schema's
        # type could contain multiple possible values. Eg:
        #     ["null", "object", "string"]
        if not isinstance(data, dict):
            return False, data

        result = {}
        successes = []
        for key, value in data.items():
            if key in schema:
                success, subdata = self.transform_recur(value, schema[key], path + [key])
                successes.append(success)
                result[key] = subdata
            else:
                # track that field has been removed
                self.removed.add(".".join(map(str, path + [key])))

        return all(successes), result

    def _transform_array(self, data, schema, path):
        # We do not necessarily have a list to transform here. The schema's
        # type could contain multiple possible values. Eg:
        #     ["null", "array", "integer"]
        if not isinstance(data, list):
            return False, data
        result = []
        successes = []
        for i, row in enumerate(data):
            success, subdata = self.transform_recur(row, schema, path + [i])
            successes.append(success)
            result.append(subdata)

        return all(successes), result

    def _transform_datetime(self, value):
        if self.integer_datetime_fmt not in VALID_DATETIME_FORMATS:
            raise Exception("Invalid integer datetime parsing option")

        if self.integer_datetime_fmt == NO_INTEGER_DATETIME_PARSING:
            return string_to_datetime(value)
        else:
            try:
                if self.integer_datetime_fmt == UNIX_SECONDS_INTEGER_DATETIME_PARSING:
                    return unix_seconds_to_datetime(value)
                else:
                    return unix_milliseconds_to_datetime(value)
            except:
                return string_to_datetime(value)

    def _transform(self, data, typ, schema, path):
        if self.pre_hook:
            data = self.pre_hook(data, typ, schema)

        if typ == "null":
            if data is None or data == "":
                return True, None
            else:
                return False, None

        elif schema.get("format") == "date-time":
            data = self._transform_datetime(data)
            if data is None:
                return False, None

            return True, data

        elif typ == "object":
            return self._transform_object(data, schema["properties"], path)

        elif typ == "array":
            return self._transform_array(data, schema["items"], path)

        elif typ == "string":
            if data != None:
                try:
                    return True, str(data)
                except:
                    return False, None
            else:
                return False, None

        elif typ == "integer":
            if isinstance(data, str):
                data = data.replace(",", "")

            try:
                return True, int(data)
            except:
                return False, None

        elif typ == "number":
            if isinstance(data, str):
                data = data.replace(",", "")

            try:
                return True, float(data)
            except:
                return False, None

        elif typ == "boolean":
            if isinstance(data, str) and data.lower() == "false":
                return True, False

            try:
                return True, bool(data)
            except:
                return False, None

        else:
            return False, None


def transform(data, schema, integer_datetime_fmt=NO_INTEGER_DATETIME_PARSING, pre_hook=None):
    """
    Applies schema (and integer_datetime_fmt, if supplied) to data, transforming
    each field in data to the type specified in schema. If no type matches a
    data field, this throws an Exception.

    This applies types in order with the exception of 'null', which is always
    applied last.

    The valid types are: integer, number, boolean, array, object, null, string,
    and string with date-time format.

    If an integer_datetime_fmt is supplied, integer values in fields with date-
    time formats are appropriately parsed as unix seconds or unix milliseconds.

    The pre_hook should be a callable that takes data, type, and schema and
    returns the transformed data to be fed into the _transform function.
    """
    transformer = Transformer(integer_datetime_fmt, pre_hook)
    return transformer.transform(data, schema)

def _transform_datetime(value, integer_datetime_fmt=NO_INTEGER_DATETIME_PARSING):
    transformer = Transformer(integer_datetime_fmt)
    return transformer._transform_datetime(value)

def resolve_schema_references(schema, refs=None):
    '''Resolves and replaces json-schema $refs with the appropriate dict.

    Recursively walks the given schema dict, converting every instance
    of $ref in a 'properties' structure with a resolved dict.

    This modifies the input schema and also returns it.

    Arguments:
        schema:
            the schema dict
        refs:
            a dict of <string, dict> which forms a store of referenced schemata

    Returns:
        schema
    '''
    refs = refs or {}
    return _resolve_schema_references(schema, RefResolver("", schema, store=refs))

def _resolve_schema_references(schema, resolver):
    if SchemaKey.ref in schema:
        reference_path = schema.pop(SchemaKey.ref, None)
        resolved = resolver.resolve(reference_path)[1]
        schema.update(resolved)
        return _resolve_schema_references(schema, resolver)

    if SchemaKey.properties in schema:
        for k, val in schema[SchemaKey.properties].items():
            schema[SchemaKey.properties][k] = _resolve_schema_references(val, resolver)

    if SchemaKey.pattern_properties in schema:
        for k, val in schema[SchemaKey.pattern_properties].items():
            schema[SchemaKey.pattern_properties][k] = _resolve_schema_references(val, resolver)

    if SchemaKey.items in schema:
        schema[SchemaKey.items] = _resolve_schema_references(schema[SchemaKey.items], resolver)

    return schema
