package org.buro.nifi.directory;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.nifi.annotation.behavior.InputRequirement;
import org.apache.nifi.annotation.behavior.SupportsBatching;
import org.apache.nifi.annotation.documentation.CapabilityDescription;
import org.apache.nifi.annotation.documentation.Tags;
import org.apache.nifi.components.PropertyDescriptor;
import org.apache.nifi.components.ValidationResult;
import org.apache.nifi.components.Validator;
import org.apache.nifi.flowfile.FlowFile;
import org.apache.nifi.processor.AbstractProcessor;
import org.apache.nifi.processor.ProcessContext;
import org.apache.nifi.processor.ProcessSession;
import org.apache.nifi.processor.Relationship;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.*;
import java.util.regex.Pattern;

@Tags({"oracle", "branch", "zone", "directory", "http", "buro"})
@CapabilityDescription("Serves a complete authenticated Oracle branch directory snapshot for BURO Insight AI. Configure Oracle connection, schema/table and all eight column mappings in Properties. Connect HandleHttpRequest to this processor and its response relationship to HandleHttpResponse using the same HTTP Context Map. Includes the Oracle JDBC driver. Never executes caller-provided SQL or grants user access.")
@InputRequirement(InputRequirement.Requirement.INPUT_REQUIRED)
@SupportsBatching
public class OracleBranchDirectory extends AbstractProcessor {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z][A-Za-z0-9_$#]{0,127}");
    private static final Pattern CODE = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,31}");
    private static final List<String> COLUMNS = List.of("DIVISIONCODE", "DIVISIONNAME", "ZONECODE", "ZONENAME", "AREACODE", "AREANAME", "BRANCHCODE", "BRANCHNAME");
    private static final Validator ID = (subject, value, context) -> result(subject, value,
            value != null && IDENTIFIER.matcher(value).matches(), "Use one unquoted Oracle identifier; SQL, dots and quotes are not allowed.");
    private static final Validator NONEMPTY = (subject, value, context) -> result(subject, value, value != null && !value.isBlank(), "A value is required.");
    private static final Validator SECRET = (subject, value, context) -> new ValidationResult.Builder().subject(subject)
            .valid(value != null && value.getBytes(StandardCharsets.UTF_8).length >= 32 && value.chars().noneMatch(c -> c < 32 || c == 127))
            .explanation("Use a dedicated token of at least 32 bytes without control characters.").build();

    public static final PropertyDescriptor URL = new PropertyDescriptor.Builder().name("oracle-jdbc-url").displayName("Oracle JDBC URL")
            .description("For example jdbc:oracle:thin:@//HOST:1521/SERVICE. Do not embed credentials in this URL; use the sensitive password property.")
            .required(true).addValidator((s, v, c) -> result(s, v, v != null && v.startsWith("jdbc:oracle:thin:@") && v.length() < 4096, "Use an Oracle thin JDBC URL without embedded credentials.")).build();
    public static final PropertyDescriptor USER = property("oracle-username", "Oracle Username", "SELECT-only database account.", null, NONEMPTY, true);
    public static final PropertyDescriptor PASSWORD = new PropertyDescriptor.Builder().name("oracle-password").displayName("Oracle Password")
            .description("Database password. Use a sensitive NiFi parameter.").required(true).sensitive(true).addValidator(NONEMPTY).build();
    public static final PropertyDescriptor SCHEMA = property("oracle-schema", "Oracle Schema", "Optional owner. Leave unset to use the account's schema or private synonym.", null, ID, false);
    public static final PropertyDescriptor TABLE = property("oracle-table", "Oracle Table", "Table or synonym containing the branch hierarchy. No SQL or quoted identifiers.", "DW_GC_BRANCHLIST2", ID, true);
    public static final PropertyDescriptor TOKEN = new PropertyDescriptor.Builder().name("directory-api-token").displayName("Directory API Token")
            .description("Dedicated bearer token shared with Django LOAN_DIRECTORY_TOKEN, at least 32 bytes. Never use the financial signing key.")
            .required(true).sensitive(true).addValidator(SECRET).build();
    public static final PropertyDescriptor PATH = property("api-path", "API Path", "Exact incoming http.request.uri to serve. Configure the same path in the HTTP listener and Django URL.", "/api/reference/branches",
            (s, v, c) -> result(s, v, v != null && v.matches("/[A-Za-z0-9/_-]{1,255}"), "Use an absolute path without query string, fragment or spaces."), true);
    public static final PropertyDescriptor TIMEOUT = number("query-timeout-seconds", "Query Timeout Seconds", "30", 1, 120);
    public static final PropertyDescriptor CONNECT_TIMEOUT = number("connect-timeout-seconds", "Connect Timeout Seconds", "10", 1, 30);
    public static final PropertyDescriptor MAX_ROWS = number("max-branches", "Maximum Branches", "5000", 1, 5000);
    public static final PropertyDescriptor FETCH_SIZE = number("fetch-size", "Fetch Size", "256", 1, 2000);
    public static final List<PropertyDescriptor> COLUMN_PROPERTIES = COLUMNS.stream().map(column -> property("column-" + column.toLowerCase(Locale.ROOT),
            column.replace("CODE", " Code").replace("NAME", " Name") + " Column", "Oracle column mapped to API field " + column + ".", column, ID, true)).toList();
    public static final Relationship RESPONSE = new Relationship.Builder().name("response").description("All HTTP responses, including authentication, validation and database errors. Route to HandleHttpResponse with status ${http.status.code}.").build();
    private static final List<PropertyDescriptor> PROPERTIES;
    static {
        var props = new ArrayList<>(List.of(URL, USER, PASSWORD, SCHEMA, TABLE, TOKEN, PATH, TIMEOUT, CONNECT_TIMEOUT, MAX_ROWS, FETCH_SIZE));
        props.addAll(COLUMN_PROPERTIES);
        PROPERTIES = List.copyOf(props);
    }
    private static ValidationResult result(String subject, String value, boolean valid, String explanation) {
        // No input echoed: validation may contain an accidentally pasted credential.
        return new ValidationResult.Builder().subject(subject).valid(valid).explanation(explanation).build();
    }
    private static PropertyDescriptor property(String name, String display, String description, String defaultValue, Validator validator, boolean required) {
        return new PropertyDescriptor.Builder().name(name).displayName(display).description(description).required(required).defaultValue(defaultValue).addValidator(validator).build();
    }
    private static PropertyDescriptor number(String name, String display, String defaultValue, int min, int max) {
        return property(name, display, "Allowed range " + min + "–" + max + ".", defaultValue, (s, v, c) -> {
            boolean valid;
            try { int n = Integer.parseInt(v); valid = n >= min && n <= max; }
            catch (RuntimeException ignored) { valid = false; }
            return result(s, v, valid, "Enter an integer between " + min + " and " + max + ".");
        }, true);
    }
    @Override protected List<PropertyDescriptor> getSupportedPropertyDescriptors() { return PROPERTIES; }
    @Override public Set<Relationship> getRelationships() { return Set.of(RESPONSE); }

    static String identifier(String value) {
        if (value == null || !IDENTIFIER.matcher(value).matches()) throw new IllegalArgumentException("Invalid configured identifier");
        return value.toUpperCase(Locale.ROOT);
    }
    static String sql(ProcessContext context) {
        String schema = context.getProperty(SCHEMA).getValue();
        String table = (schema == null || schema.isBlank() ? "" : identifier(schema) + ".") + identifier(context.getProperty(TABLE).getValue());
        var selects = new ArrayList<String>();
        for (int i = 0; i < COLUMNS.size(); i++) selects.add("TRIM(" + identifier(context.getProperty(COLUMN_PROPERTIES.get(i)).getValue()) + ") AS " + COLUMNS.get(i));
        return "SELECT DISTINCT " + String.join(", ", selects) + " FROM " + table + " ORDER BY BRANCHCODE, ZONECODE";
    }
    protected Connection openConnection(ProcessContext context) throws SQLException {
        Properties properties = new Properties();
        properties.setProperty("user", context.getProperty(USER).getValue());
        properties.setProperty("password", context.getProperty(PASSWORD).getValue());
        properties.setProperty("oracle.net.CONNECT_TIMEOUT", Integer.toString(context.getProperty(CONNECT_TIMEOUT).asInteger() * 1000));
        properties.setProperty("oracle.jdbc.ReadTimeout", Integer.toString((context.getProperty(TIMEOUT).asInteger() + 5) * 1000));
        // Explicit driver instance works within the isolated NAR classloader.
        Connection connection = new oracle.jdbc.OracleDriver().connect(context.getProperty(URL).getValue(), properties);
        if (connection == null) throw new SQLException("Unsupported JDBC URL");
        return connection;
    }
    static void validateRows(List<Map<String, String>> rows) {
        if (rows.isEmpty()) throw new IllegalArgumentException("Empty snapshot");
        Set<String> branches = new HashSet<>();
        Map<String, Object> zones = new HashMap<>(), areas = new HashMap<>(), divisions = new HashMap<>();
        for (var row : rows) {
            for (String prefix : List.of("DIVISION", "ZONE", "AREA", "BRANCH")) {
                String code = row.get(prefix + "CODE"), name = row.get(prefix + "NAME");
                boolean optional = (prefix.equals("DIVISION") || prefix.equals("AREA")) && code.isEmpty() && name.isEmpty();
                if (!optional && (!CODE.matcher(code).matches() || name.isBlank() || name.length() > 150 || name.chars().anyMatch(c -> c < 32 || c == 127))) throw new IllegalArgumentException("Invalid directory row");
            }
            if (!branches.add(row.get("BRANCHCODE"))) throw new IllegalArgumentException("Duplicate branch code");
            consistent(zones, row.get("ZONECODE"), List.of(row.get("ZONENAME"), row.get("DIVISIONCODE"), row.get("DIVISIONNAME")));
            consistent(areas, row.get("AREACODE"), List.of(row.get("AREANAME"), row.get("ZONECODE")));
            consistent(divisions, row.get("DIVISIONCODE"), row.get("DIVISIONNAME"));
        }
    }
    private static void consistent(Map<String, Object> seen, String code, Object value) {
        if (code.isEmpty()) return;
        Object old = seen.putIfAbsent(code, value);
        if (old != null && !old.equals(value)) throw new IllegalArgumentException("Conflicting hierarchy");
    }
    @Override public void onTrigger(ProcessContext context, ProcessSession session) {
        FlowFile file = session.get();
        if (file == null) return;
        Set<String> authKeys = new HashSet<>();
        file.getAttributes().keySet().stream().filter(k -> k.equalsIgnoreCase("http.headers.authorization")).forEach(authKeys::add);
        String supplied = authKeys.size() == 1 ? file.getAttribute(authKeys.iterator().next()) : "";
        int status = 503;
        Map<String, Object> body = error("directory_unavailable");
        try {
            String token = context.getProperty(TOKEN).getValue();
            if (token == null || token.getBytes(StandardCharsets.UTF_8).length < 32) {
                status = 503;
            } else if (!MessageDigest.isEqual(("Bearer " + token).getBytes(StandardCharsets.UTF_8), Objects.requireNonNullElse(supplied, "").getBytes(StandardCharsets.UTF_8))) {
                status = 401; body = error("unauthorized");
            } else if (!context.getProperty(PATH).getValue().equals(file.getAttribute("http.request.uri"))) {
                status = 404; body = error("not_found");
            } else if (!"GET".equals(file.getAttribute("http.method"))) {
                status = 405; body = error("method_not_allowed");
            } else if (!Objects.requireNonNullElse(file.getAttribute("http.query.string"), "").isBlank()) {
                status = 400; body = error("query_parameters_not_supported");
            } else {
                List<Map<String, String>> rows = new ArrayList<>();
                int maxRows = context.getProperty(MAX_ROWS).asInteger();
                try (Connection connection = openConnection(context); var statement = connection.prepareStatement(sql(context))) {
                    statement.setQueryTimeout(context.getProperty(TIMEOUT).asInteger());
                    statement.setFetchSize(context.getProperty(FETCH_SIZE).asInteger());
                    statement.setMaxRows(maxRows + 1);
                    try (var result = statement.executeQuery()) {
                        while (result.next()) {
                            if (rows.size() == maxRows) throw new IllegalArgumentException("Snapshot too large");
                            Map<String, String> row = new LinkedHashMap<>();
                            for (String column : COLUMNS) row.put(column, Objects.requireNonNullElse(result.getString(column), "").trim());
                            rows.add(row);
                        }
                    }
                }
                validateRows(rows);
                status = 200;
                // This is the stable logical source contract consumed by Django;
                // the physical schema/table and columns are independently configurable.
                body = Map.of("success", true, "complete", true, "version", 1, "source", "DW_GC_BRANCHLIST2", "row_count", rows.size(), "rows", rows);
            }
        } catch (Exception ignored) {
            status = 503; body = error("directory_unavailable");
            // Do not log SQLException messages, credentials or partial rows.
            getLogger().warn("Oracle directory request failed; check connection settings and source data with the database administrator.");
        }
        byte[] bytes;
        try {
            bytes = JSON.writeValueAsBytes(body);
            if (bytes.length > 4_000_000) { status = 503; bytes = JSON.writeValueAsBytes(error("directory_too_large")); }
        } catch (Exception impossible) { throw new IllegalStateException("Unable to encode directory response"); }
        file = session.removeAllAttributes(file, authKeys);
        file = session.putAllAttributes(file, Map.of("http.status.code", Integer.toString(status), "mime.type", "application/json"));
        byte[] output = bytes;
        file = session.write(file, stream -> stream.write(output));
        session.transfer(file, RESPONSE);
    }
    private static Map<String, Object> error(String code) { return Map.of("success", false, "error", code); }
}
