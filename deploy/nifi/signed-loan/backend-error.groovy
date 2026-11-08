import org.apache.nifi.processor.io.OutputStreamCallback
import java.nio.charset.StandardCharsets

def flowFile = session.get()
if (!flowFile) return
byte[] bytes = '{"ok":false,"error":"loan_backend_failed"}'.getBytes(StandardCharsets.UTF_8)
flowFile = session.write(flowFile, { output -> output.write(bytes) } as OutputStreamCallback)
flowFile = session.removeAllAttributes(flowFile, flowFile.attributes.keySet().findAll {
    it.startsWith('auth.') || it.startsWith('loan.access.') || it.toLowerCase().contains('loan_access_token')
} as Set)
flowFile = session.putAllAttributes(flowFile, ['http.status.code': '503', 'mime.type': 'application/json'])
session.transfer(flowFile, REL_SUCCESS)
