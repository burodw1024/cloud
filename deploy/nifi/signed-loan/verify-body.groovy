// ExecuteScript/Groovy dynamic property expectedTool: fixed registered MCP tool name.
// Configure LOAN_ACCESS_SIGNING_KEY in the NiFi service environment, never in the flow.
import org.apache.nifi.processor.io.InputStreamCallback
import org.apache.nifi.processor.io.OutputStreamCallback

def flowFile = session.get()
if (!flowFile) return
def remove = flowFile.attributes.keySet().findAll {
    it.startsWith('auth.') || it.startsWith('loan.access.') ||
    it.toLowerCase().contains('loan_access_token') || it.equalsIgnoreCase('http.headers.authorization')
} as Set
flowFile = session.removeAllAttributes(flowFile, remove)
def relation = REL_FAILURE
def result = [ok: false, error: 'loan_authorization_failed']
def attributes = ['http.status.code': '403', 'mime.type': 'application/json']
try {
    SignedLoanScope.require(flowFile.getAttribute('http.method') == 'POST', 'post_required')
    SignedLoanScope.require(!flowFile.getAttribute('http.query.string'), 'query_string_not_supported')
    SignedLoanScope.require(flowFile.size <= SignedLoanScope.MAX_REQUEST, 'request_too_large')
    String raw
    session.read(flowFile, { input ->
        byte[] bytes = input.readNBytes(SignedLoanScope.MAX_REQUEST + 1)
        SignedLoanScope.require(bytes.length <= SignedLoanScope.MAX_REQUEST, 'request_too_large')
        raw = new String(bytes, StandardCharsets.UTF_8)
    } as InputStreamCallback)
    String tool = expectedTool.value
    def grant = SignedLoanScope.verify(SignedLoanScope.object(raw),
        System.getenv('LOAN_ACCESS_SIGNING_KEY'), tool, System.currentTimeMillis().intdiv(1000) as long)
    // Rewrite to one unambiguous JSON object; remove the token before custom SQL processors.
    result = grant.query
    attributes.putAll(['auth.branch_codes': JsonOutput.toJson(grant.receipt.branch_codes),
        'loan.access.receipt': JsonOutput.toJson(grant.receipt), 'loan.access.verified': 'true',
        'loan.access.tool': tool, 'http.status.code': '200'])
    relation = REL_SUCCESS
} catch (ScopeFailure e) {
    attributes['http.status.code'] = e.status.toString()
    result = [ok: false, error: e.code]
} catch (Exception ignored) {
    attributes['http.status.code'] = '503'
    result = [ok: false, error: 'loan_authorization_unavailable']
}
byte[] bytes = JsonOutput.toJson(result).getBytes(StandardCharsets.UTF_8)
flowFile = session.write(flowFile, { output -> output.write(bytes) } as OutputStreamCallback)
flowFile = session.putAllAttributes(flowFile, attributes)
session.transfer(flowFile, relation)
