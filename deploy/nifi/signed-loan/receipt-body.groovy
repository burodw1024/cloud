// Success path ONLY, after a processor that enforces auth.branch_codes before SQL aggregation.
import org.apache.nifi.processor.io.InputStreamCallback
import org.apache.nifi.processor.io.OutputStreamCallback

def flowFile = session.get()
if (!flowFile) return
def relation = REL_FAILURE
def status = '403'
def result = [ok: false, error: 'loan_response_scope_unverified']
try {
    SignedLoanScope.require(flowFile.getAttribute('loan.access.verified') == 'true', 'verified_scope_missing')
    Map receipt = SignedLoanScope.object(flowFile.getAttribute('loan.access.receipt') ?: '{}')
    SignedLoanScope.require(flowFile.getAttribute('auth.branch_codes') == JsonOutput.toJson(receipt.branch_codes), 'verified_scope_changed')
    SignedLoanScope.require(flowFile.size <= SignedLoanScope.MAX_RESPONSE, 'response_too_large')
    String raw
    session.read(flowFile, { input ->
        byte[] bytes = input.readNBytes(SignedLoanScope.MAX_RESPONSE + 1)
        SignedLoanScope.require(bytes.length <= SignedLoanScope.MAX_RESPONSE, 'response_too_large')
        raw = new String(bytes, StandardCharsets.UTF_8)
    } as InputStreamCallback)
    result = SignedLoanScope.receiptResponse(SignedLoanScope.object(raw), receipt)
    relation = REL_SUCCESS
    status = '200'
} catch (ScopeFailure e) { result = [ok: false, error: e.code]; status = e.status.toString() }
catch (Exception ignored) { result = [ok: false, error: 'loan_response_unavailable']; status = '503' }
byte[] bytes = JsonOutput.toJson(result).getBytes(StandardCharsets.UTF_8)
flowFile = session.write(flowFile, { output -> output.write(bytes) } as OutputStreamCallback)
flowFile = session.removeAllAttributes(flowFile, flowFile.attributes.keySet().findAll {
    it.startsWith('auth.') || it.startsWith('loan.access.') || it.toLowerCase().contains('loan_access_token')
} as Set)
flowFile = session.putAllAttributes(flowFile, ['http.status.code': status, 'mime.type': 'application/json'])
session.transfer(flowFile, relation)
