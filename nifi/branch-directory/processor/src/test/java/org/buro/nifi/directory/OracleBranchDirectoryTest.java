package org.buro.nifi.directory;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.nifi.processor.ProcessContext;
import org.apache.nifi.util.TestRunner;
import org.apache.nifi.util.TestRunners;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.*;
import java.util.*;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class OracleBranchDirectoryTest {
    static final String TOKEN = "disposable-test-token-at-least-32-bytes";
    static class Processor extends OracleBranchDirectory {
        Connection connection;
        int calls;
        boolean fail;
        @Override protected Connection openConnection(ProcessContext context) throws SQLException {
            calls++;
            if (fail) throw new SQLException("secret-password-and-private-details");
            return connection;
        }
    }
    Processor processor;
    TestRunner runner;
    Connection connection;
    PreparedStatement statement;
    ResultSet result;
    List<Map<String, String>> rows;
    int index;
    final List<String> columns = List.of("DIVISIONCODE", "DIVISIONNAME", "ZONECODE", "ZONENAME", "AREACODE", "AREANAME", "BRANCHCODE", "BRANCHNAME");

    @BeforeEach void setup() throws Exception {
        processor = new Processor();
        runner = TestRunners.newTestRunner(processor);
        runner.setProperty(OracleBranchDirectory.URL, "jdbc:oracle:thin:@//oracle.example:1521/ORCLPDB1");
        runner.setProperty(OracleBranchDirectory.USER, "directory_reader");
        runner.setProperty(OracleBranchDirectory.PASSWORD, "secret-password-and-private-details");
        runner.setProperty(OracleBranchDirectory.TOKEN, TOKEN);
        connection = mock(Connection.class);
        statement = mock(PreparedStatement.class);
        result = mock(ResultSet.class);
        processor.connection = connection;
        when(connection.prepareStatement(anyString())).thenReturn(statement);
        when(statement.executeQuery()).thenReturn(result);
        rows = new ArrayList<>(List.of(row("00253")));
        index = -1;
        when(result.next()).thenAnswer(inv -> ++index < rows.size());
        when(result.getString(anyString())).thenAnswer(inv -> rows.get(index).get(inv.getArgument(0, String.class)));
    }
    Map<String, String> row(String code) {
        return new HashMap<>(Map.of("DIVISIONCODE", "01", "DIVISIONNAME", "Division", "ZONECODE", "007", "ZONENAME", "Zone", "AREACODE", "0004", "AREANAME", "Area", "BRANCHCODE", code, "BRANCHNAME", "Branch"));
    }
    Map<String, String> headers() {
        return new HashMap<>(Map.of("http.headers.Authorization", "Bearer " + TOKEN, "http.method", "GET", "http.request.uri", "/api/reference/branches", "http.context.identifier", "retained-context"));
    }
    JsonNode run(Map<String, String> headers, int status) throws Exception {
        runner.enqueue(new byte[0], headers);
        runner.run();
        runner.assertAllFlowFilesTransferred(OracleBranchDirectory.RESPONSE, 1);
        var file = runner.getFlowFilesForRelationship(OracleBranchDirectory.RESPONSE).getFirst();
        file.assertAttributeEquals("http.status.code", Integer.toString(status));
        file.assertAttributeEquals("http.context.identifier", "retained-context");
        file.assertAttributeEquals("mime.type", "application/json");
        assertTrue(file.getAttributes().keySet().stream().noneMatch(k -> k.equalsIgnoreCase("http.headers.authorization")));
        String text = new String(file.toByteArray(), java.nio.charset.StandardCharsets.UTF_8);
        assertFalse(text.contains(TOKEN));
        assertFalse(text.contains("secret-password-and-private-details"));
        return new ObjectMapper().readTree(text);
    }
    @Test void completeSnapshotPreservesCodesAndClosesResources() throws Exception {
        JsonNode body = run(headers(), 200);
        assertTrue(body.get("complete").asBoolean());
        assertEquals("DW_GC_BRANCHLIST2", body.get("source").asText());
        assertEquals(1, body.get("row_count").asInt());
        assertEquals("00253", body.at("/rows/0/BRANCHCODE").asText());
        verify(statement).setQueryTimeout(30);
        verify(statement).setMaxRows(5001);
        verify(statement).setFetchSize(256);
        verify(result).close(); verify(statement).close(); verify(connection).close();
    }
    @Test void missingTokenDoesNotConnect() throws Exception {
        var h = headers(); h.remove("http.headers.Authorization"); run(h, 401); assertEquals(0, processor.calls);
    }
    @Test void wrongTokenDoesNotConnect() throws Exception {
        var h = headers(); h.put("http.headers.Authorization", "Bearer wrong"); run(h, 401); assertEquals(0, processor.calls);
    }
    @Test void duplicateAuthorizationRejectedAndRemoved() throws Exception {
        var h = headers(); h.put("http.headers.authorization", "Bearer " + TOKEN); run(h, 401); assertEquals(0, processor.calls);
    }
    @Test void wrongMethodDoesNotConnect() throws Exception {
        var h = headers(); h.put("http.method", "POST"); run(h, 405); assertEquals(0, processor.calls);
    }
    @Test void pathIsConfigurable() throws Exception {
        runner.setProperty(OracleBranchDirectory.PATH, "/api/custom/directory");
        var h = headers(); h.put("http.request.uri", "/api/custom/directory"); run(h, 200);
    }
    @Test void wrongPathDoesNotConnect() throws Exception {
        var h = headers(); h.put("http.request.uri", "/other"); run(h, 404); assertEquals(0, processor.calls);
    }
    @Test void queryParametersNeverBecomeSQL() throws Exception {
        var h = headers(); h.put("http.query.string", "table=SECRET"); run(h, 400); assertEquals(0, processor.calls);
    }
    @Test void schemaTableAndColumnsAreConfigurable() throws Exception {
        runner.setProperty(OracleBranchDirectory.SCHEMA, "dw_owner");
        runner.setProperty(OracleBranchDirectory.TABLE, "branch_master");
        runner.setProperty(OracleBranchDirectory.COLUMN_PROPERTIES.get(6), "branch_id");
        run(headers(), 200);
        verify(connection).prepareStatement(argThat(sql -> sql.contains("FROM DW_OWNER.BRANCH_MASTER") && sql.contains("TRIM(BRANCH_ID) AS BRANCHCODE") && !sql.contains("TO_NUMBER")));
    }
    @Test void injectedIdentifiersInvalid() {
        for (String value : List.of("T; DROP TABLE T", "OWNER.TABLE", "\"MixedCase\"", "T WHERE 1=1", " ")) {
            runner.setProperty(OracleBranchDirectory.TABLE, value); runner.assertNotValid();
        }
    }
    @Test void weakTokenAndBadURLInvalid() {
        runner.setProperty(OracleBranchDirectory.TOKEN, "short"); runner.assertNotValid();
        runner.setProperty(OracleBranchDirectory.TOKEN, TOKEN);
        runner.setProperty(OracleBranchDirectory.URL, "jdbc:oracle:thin:user/password@host"); runner.assertNotValid();
        assertTrue(OracleBranchDirectory.PASSWORD.isSensitive()); assertTrue(OracleBranchDirectory.TOKEN.isSensitive());
    }
    @Test void defaultsAndBoundsValidated() {
        runner.assertValid();
        runner.setProperty(OracleBranchDirectory.MAX_ROWS, "5001"); runner.assertNotValid();
        runner.setProperty(OracleBranchDirectory.MAX_ROWS, "5000");
        runner.setProperty(OracleBranchDirectory.TIMEOUT, "0"); runner.assertNotValid();
    }
    @Test void databaseFailureReturnsNoDetails() throws Exception {
        processor.fail = true; JsonNode body = run(headers(), 503); assertFalse(body.has("rows"));
    }
    @Test void emptySnapshotRejected() throws Exception { rows.clear(); assertFalse(run(headers(), 503).has("rows")); }
    @Test void truncatedSnapshotRejected() throws Exception {
        runner.setProperty(OracleBranchDirectory.MAX_ROWS, "1"); rows.add(row("00254")); assertFalse(run(headers(), 503).has("rows")); verify(connection).close();
    }
    @Test void nullBranchCodeRejected() throws Exception { rows.getFirst().put("BRANCHCODE", null); run(headers(), 503); }
    @Test void overlongCodeRejectedRatherThanTruncated() throws Exception { rows.getFirst().put("BRANCHCODE", "1".repeat(33)); run(headers(), 503); }
    @Test void optionalHierarchyCanBeNull() throws Exception {
        for (String key : List.of("DIVISIONCODE", "DIVISIONNAME", "AREACODE", "AREANAME")) rows.getFirst().put(key, null);
        assertEquals("", run(headers(), 200).at("/rows/0/DIVISIONCODE").asText());
    }
    @Test void duplicateNamesAllowed() throws Exception { rows.add(row("00254")); assertEquals(2, run(headers(), 200).get("row_count").asInt()); }
    @Test void conflictingBranchRejected() throws Exception { rows.add(row("00253")); run(headers(), 503); }
    @Test void conflictingZoneRejected() throws Exception { var r = row("00254"); r.put("ZONENAME", "Different"); rows.add(r); run(headers(), 503); }
    @Test void conflictingAreaRejected() throws Exception { var r = row("00254"); r.put("AREANAME", "Different"); rows.add(r); run(headers(), 503); }
    @Test void conflictingDivisionRejected() throws Exception { var r = row("00254"); r.put("ZONECODE", "008"); r.put("AREACODE", "0005"); r.put("DIVISIONNAME", "Different"); rows.add(r); run(headers(), 503); }
}
