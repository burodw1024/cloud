import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import java.nio.charset.StandardCharsets
import java.security.MessageDigest

class ScopeFailure extends RuntimeException {
    final int status
    final String code
    ScopeFailure(int status, String code) { super(code); this.status = status; this.code = code }
}

/** Standalone contract shared by the NiFi request/response scripts and JVM tests. */
class SignedLoanScope {
    static final int MAX_REQUEST = 1048576
    static final int MAX_RESPONSE = 33554432
    static final String TOKEN = 'loan_access_token'
    static final String CODE = '[A-Za-z0-9][A-Za-z0-9._-]{0,31}'
    static void require(boolean condition, String code = 'invalid_loan_authorization') {
        if (!condition) throw new ScopeFailure(403, code)
    }
    static boolean integer(Object value) {
        value instanceof Integer || value instanceof Long || value instanceof BigInteger
    }
    static Map object(String raw) {
        try {
            def value = new JsonSlurper().parseText(raw)
            require(value instanceof Map, 'invalid_json_object')
            return value as Map
        } catch (ScopeFailure e) { throw e }
        catch (Exception ignored) { throw new ScopeFailure(400, 'invalid_json_object') }
    }
    static Object ordered(Object value) {
        if (value instanceof Map) {
            def result = new TreeMap<String, Object>()
            value.each { k, v -> result[k.toString()] = ordered(v) }
            return result
        }
        if (value instanceof List) return value.collect { ordered(it) }
        return value
    }
    static String canonical(Object value) { JsonOutput.toJson(ordered(value)) }
    static byte[] decode(String value) {
        require(value ==~ /[A-Za-z0-9_-]+/)
        try { return Base64.urlDecoder.decode(value) }
        catch (Exception ignored) { throw new ScopeFailure(403, 'invalid_loan_authorization') }
    }
    static Map verify(Map payload, String key, String tool, long now) {
        if (!key || key.getBytes(StandardCharsets.UTF_8).length < 32)
            throw new ScopeFailure(503, 'loan_signing_not_configured')
        require(tool ==~ /[A-Za-z0-9][A-Za-z0-9._\/-]{0,149}/, 'invalid_endpoint_tool')
        require(payload[TOKEN] instanceof String)
        String token = payload[TOKEN]
        require(token.length() <= 400000)
        String[] parts = token.split('\\.', -1)
        require(parts.length == 3)
        Map header = object(new String(decode(parts[0]), StandardCharsets.UTF_8))
        require(header.alg == 'HS256' && !header.containsKey('crit') && !header.containsKey('b64'))
        Mac mac = Mac.getInstance('HmacSHA256')
        mac.init(new SecretKeySpec(key.getBytes(StandardCharsets.UTF_8), 'HmacSHA256'))
        require(MessageDigest.isEqual(mac.doFinal((parts[0] + '.' + parts[1]).getBytes(StandardCharsets.US_ASCII)), decode(parts[2])))
        Map claims = object(new String(decode(parts[1]), StandardCharsets.UTF_8))
        require(integer(claims.v) && claims.v == 1)
        require(claims.iss == 'buro-insight-ai' && claims.aud == 'buro-loan-query' && claims.tool == tool)
        require(claims.sub instanceof String && claims.sub ==~ /[1-9][0-9]{0,18}/)
        require(claims.jti instanceof String && claims.jti ==~ /[0-9a-f]{32}/)
        require(claims.scope_version instanceof String && claims.scope_version ==~ /[0-9a-f]{64}/)
        require(['iat', 'nbf', 'exp'].every { integer(claims[it]) })
        require(claims.nbf == claims.iat && claims.iat <= now && now < claims.exp)
        require(claims.exp - claims.iat > 0 && claims.exp - claims.iat <= 180)
        require(claims.branches instanceof List && claims.branches.size() >= 1 && claims.branches.size() <= 5000)
        require(claims.branches.every { it instanceof String && it ==~ CODE })
        require(claims.branches == claims.branches.toSet().toList().sort())
        require(claims.query instanceof Map)
        Map supplied = new LinkedHashMap(payload)
        supplied.remove(TOKEN)
        require(!supplied.containsKey('auth.branch_codes') && !supplied.containsKey('authorized_branches'))
        require(canonical(supplied) == canonical(claims.query), 'query_does_not_match_authorization')
        ['branch_code', 'branchCode'].each { name ->
            if (supplied.containsKey(name)) require(supplied[name] instanceof String && claims.branches.contains(supplied[name]), 'branch_outside_authorization')
        }
        return [query: supplied, receipt: [version: 1, request_id: claims.jti, subject: claims.sub,
                scope_version: claims.scope_version, branch_codes: claims.branches]]
    }
    static void checkRows(Object value, Set<String> allowed) {
        if (value instanceof Map) value.each { k, v ->
            require(k != TOKEN, 'backend_echoed_authorization')
            if (k in ['BRANCHCODE', 'branchCode', 'branch_code'] && v != null)
                require(v instanceof String && allowed.contains(v), 'backend_branch_outside_authorization')
            if (k == 'authorized_branch_count') require(integer(v) && v >= 0 && v <= allowed.size(), 'backend_scope_exceeds_authorization')
            if (k != 'authorization') checkRows(v, allowed)
        }
        else if (value instanceof List) value.each { checkRows(it, allowed) }
        else if (value instanceof String && (value.trim().startsWith('{') || value.trim().startsWith('['))) {
            def decoded
            try { decoded = new JsonSlurper().parseText(value) } catch (Exception ignored) { return }
            if (decoded instanceof Map || decoded instanceof List) checkRows(decoded, allowed)
        }
    }
    static Map receiptResponse(Map data, Map receipt) {
        require(receipt.version == 1 && receipt.branch_codes instanceof List && !receipt.branch_codes.empty,
                'verified_scope_missing')
        require(data.success != false && data.ok != false, 'backend_report_failed')
        if (data.containsKey('authorization')) require(canonical(data.authorization) == canonical(receipt), 'backend_receipt_conflict')
        checkRows(data, receipt.branch_codes as Set<String>)
        data.authorization = receipt
        return data
    }
}
