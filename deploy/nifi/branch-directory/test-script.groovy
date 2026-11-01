/* Run with Groovy 4, groovy-json and nifi-api on the classpath.
 * LOAN_DIRECTORY_TOKEN must be set to a disposable >=32-byte test value.
 */
import groovy.json.JsonSlurper
class Result implements AutoCloseable {
    List rows; int pos = -1
    boolean next() { ++pos < rows.size() }
    String getString(String key) { rows[pos][key] }
    void close() {}
}
class Statement implements AutoCloseable {
    List rows
    void setQueryTimeout(int n) { assert n == 30 }
    void setFetchSize(int n) { assert n == 256 }
    void setMaxRows(int n) { assert n == 5001 }
    Result executeQuery() { new Result(rows: rows) }
    void close() {}
}
class Connection implements AutoCloseable {
    List rows
    Statement prepareStatement(String sql) {
        assert sql.contains('FROM DW_GC_BRANCHLIST2 ORDER BY BRANCHCODE, ZONECODE')
        assert !sql.contains('TO_NUMBER')
        new Statement(rows: rows)
    }
    void close() {}
}
class Pool {
    List rows; int calls = 0
    Connection getConnection() { calls++; new Connection(rows: rows) }
}
def file = new File(args[0])
def row = [DIVISIONCODE:'01', DIVISIONNAME:'Division', ZONECODE:'007', ZONENAME:'Zone', AREACODE:'0004', AREANAME:'Area', BRANCHCODE:'00253', BRANCHNAME:'Branch']
def runCase = { attributes, rows ->
    def pool = new Pool(rows: rows)
    def flow = [attributes: attributes + ['http.context.identifier':'context-preserved']]
    def out = new ByteArrayOutputStream()
    def session = new Expando(get: { -> flow }, removeAllAttributes: { f, keys -> keys.each { f.attributes.remove(it) }; f },
        putAllAttributes: { f, values -> f.attributes.putAll(values); f },
        write: { f, callback -> callback.process(out); f }, transfer: { f, relationship -> assert relationship == 'success' })
    def lookup = [getControllerService: { id -> assert id == 'pool-id'; pool }]
    new GroovyShell(new Binding([session: session, context:[controllerServiceLookup:lookup], dbcpServiceId:[value:'pool-id'], REL_SUCCESS:'success'])).evaluate(file)
    assert flow.attributes['http.context.identifier'] == 'context-preserved'
    assert !flow.attributes.containsKey('http.headers.Authorization')
    [status: flow.attributes['http.status.code'], body:new JsonSlurper().parse(out.toByteArray()), calls:pool.calls]
}
def headers = ['http.headers.Authorization':'Bearer ' + System.getenv('LOAN_DIRECTORY_TOKEN'), 'http.method':'GET', 'http.request.uri':'/api/reference/branches']
def result = runCase(headers, [row])
assert result.status == '200': result
assert result.body.rows[0].BRANCHCODE == '00253'
assert result.body.complete && result.body.row_count == 1
assert !result.body.toString().contains(System.getenv('LOAN_DIRECTORY_TOKEN'))
result = runCase(headers + ['http.headers.Authorization':'bad'], [row]); assert result.status == '401' && result.calls == 0
result = runCase(headers + ['http.method':'POST'], [row]); assert result.status == '405' && result.calls == 0
result = runCase(headers + ['http.request.uri':'/other'], [row]); assert result.status == '404' && result.calls == 0
result = runCase(headers + ['http.query.string':'sql=select'], [row]); assert result.status == '400' && result.calls == 0
result = runCase(headers, []); assert result.status == '503' && !result.body.containsKey('rows')
result = runCase(headers, [row + [BRANCHCODE:'1'*33]]); assert result.status == '503'
result = runCase(headers, [row, row + [ZONENAME:'Conflict', BRANCHCODE:'00254']]); assert result.status == '503'
result = runCase(headers, [row, row]); assert result.status == '503'
result = runCase(headers, [row, row + [BRANCHCODE:'00254']]); assert result.status == '200'
result = runCase(headers, [row + [DIVISIONCODE:null, DIVISIONNAME:null, AREACODE:null, AREANAME:null]]); assert result.status == '200'
result = runCase(headers, (1..5001).collect { row + [BRANCHCODE:it.toString()] }); assert result.status == '503'
println('12 NiFi script contract checks passed (mock JDBC/session, NiFi 2.4 API).')
