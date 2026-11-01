/* ExecuteScript / Groovy. Dynamic property: dbcpServiceId = DBCP service UUID.
 * Secret: LOAN_DIRECTORY_TOKEN in the NiFi process environment (>=32 bytes).
 * Keep the incoming FlowFile so http.context.identifier reaches HandleHttpResponse.
 */
import groovy.json.JsonOutput
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import org.apache.nifi.processor.io.OutputStreamCallback

 def flowFile = session.get()
 if (!flowFile) return
 def status = 503
 def body = [success: false, error: 'directory_unavailable']
 def token = System.getenv('LOAN_DIRECTORY_TOKEN') ?: ''
 def attributes = flowFile.attributes
 Set<String> authKeys = attributes.keySet().findAll { it.equalsIgnoreCase('http.headers.authorization') } as Set
 def supplied = authKeys.size() == 1 ? attributes[authKeys.iterator().next()] : ''
 try {
     if (token.getBytes(StandardCharsets.UTF_8).length < 32) {
         status = 503
     } else if (!MessageDigest.isEqual(('Bearer ' + token).getBytes(StandardCharsets.UTF_8), (supplied ?: '').getBytes(StandardCharsets.UTF_8))) {
         status = 401; body = [success: false, error: 'unauthorized']
     } else if (attributes['http.request.uri'] != '/api/reference/branches') {
         status = 404; body = [success: false, error: 'not_found']
     } else if (attributes['http.method'] != 'GET') {
         status = 405; body = [success: false, error: 'method_not_allowed']
     } else if (attributes['http.query.string']) {
         status = 400; body = [success: false, error: 'query_parameters_not_supported']
     } else {
         def service = context.controllerServiceLookup.getControllerService(dbcpServiceId.value)
         def columns = ['DIVISIONCODE', 'DIVISIONNAME', 'ZONECODE', 'ZONENAME', 'AREACODE', 'AREANAME', 'BRANCHCODE', 'BRANCHNAME']
         // Resolve the fixed table through the pool account's schema or a DBA-created
         // synonym. No HTTP value, arbitrary schema or caller SQL is interpolated.
         def query = 'SELECT DISTINCT ' + columns.collect { 'TRIM(' + it + ') AS ' + it }.join(', ') + ' FROM DW_GC_BRANCHLIST2 ORDER BY BRANCHCODE, ZONECODE'
         def rows = []
         service.getConnection().withCloseable { connection ->
             connection.prepareStatement(query).withCloseable { statement ->
                 statement.setQueryTimeout(30)
                 statement.setFetchSize(256)
                 statement.setMaxRows(5001)
                 statement.executeQuery().withCloseable { result ->
                     while (result.next()) {
                         if (rows.size() == 5000) throw new IllegalArgumentException('snapshot too large')
                         def row = [:]
                         columns.each { key -> row[key] = result.getString(key)?.trim() ?: '' }
                         ['DIVISION', 'ZONE', 'AREA', 'BRANCH'].each { prefix ->
                             def code = row[prefix + 'CODE']; def name = row[prefix + 'NAME']
                             def optional = prefix in ['DIVISION', 'AREA'] && !code && !name
                             if (!optional && (!(code ==~ /[A-Za-z0-9][A-Za-z0-9._-]{0,31}/) || !name || name.length() > 150 || name.toCharArray().any { ((int) it) < 32 })) {
                                 throw new IllegalArgumentException('invalid directory row')
                             }
                         }
                         rows.add(row)
                     }
                 }
             }
         }
         if (!rows) throw new IllegalArgumentException('empty snapshot')
         def seenBranches = [] as Set; def zones = [:]; def areas = [:]; def divisions = [:]
         rows.each { row ->
             if (!seenBranches.add(row.BRANCHCODE)) throw new IllegalArgumentException('conflicting branch')
             [[zones, row.ZONECODE, [row.ZONENAME, row.DIVISIONCODE, row.DIVISIONNAME]],
              [areas, row.AREACODE, [row.AREANAME, row.ZONECODE]],
              [divisions, row.DIVISIONCODE, row.DIVISIONNAME]].each { entry ->
                 def (map, code, value) = entry
                 if (code && map.containsKey(code) && map[code] != value) throw new IllegalArgumentException('conflicting hierarchy')
                 if (code) map[code] = value
             }
         }
         status = 200
         body = [success: true, complete: true, version: 1, source: 'DW_GC_BRANCHLIST2', row_count: rows.size(), rows: rows]
     }
 } catch (Exception ignored) {
     // No SQL exception, endpoint credential, or partial directory in responses/logs.
     status = 503; body = [success: false, error: 'directory_unavailable']
 }
 def bytes = JsonOutput.toJson(body).getBytes(StandardCharsets.UTF_8)
 if (bytes.length > 4000000) {
     status = 503
     bytes = JsonOutput.toJson([success: false, error: 'directory_too_large']).getBytes(StandardCharsets.UTF_8)
 }
 flowFile = session.removeAllAttributes(flowFile, authKeys)
 flowFile = session.putAllAttributes(flowFile, ['http.status.code': status.toString(), 'mime.type': 'application/json'])
 def output = bytes
 flowFile = session.write(flowFile, { stream -> stream.write(output) } as OutputStreamCallback)
 session.transfer(flowFile, REL_SUCCESS)
