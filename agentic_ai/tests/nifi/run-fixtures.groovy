import groovy.json.JsonSlurper
import groovy.json.JsonOutput

class FakeFile {
    Map attributes
    byte[] content
    long getSize() { content.length }
    String getAttribute(String name) { attributes[name] }
}
class FakeSession {
    FakeFile current
    String relation
    FakeFile get() { current }
    void read(FakeFile f, Object cb) { cb.process(new ByteArrayInputStream(f.content)) }
    FakeFile write(FakeFile f, Object cb) {
        def out = new ByteArrayOutputStream(); cb.process(out)
        current = new FakeFile(attributes:new LinkedHashMap(f.attributes), content:out.toByteArray()); current
    }
    FakeFile removeAllAttributes(FakeFile f, Set keys) {
        current = new FakeFile(attributes:f.attributes.findAll { k,v -> !keys.contains(k) },content:f.content);current
    }
    FakeFile putAllAttributes(FakeFile f, Map attrs) {
        current = new FakeFile(attributes:new LinkedHashMap(f.attributes)+attrs,content:f.content);current
    }
    void transfer(FakeFile f, Object rel) { current=f; relation=rel.toString() }
}

def root = new File(args[0])
def cases = new JsonSlurper().parse(new File(args[1]))
def output=[]
cases.each { item ->
    def loader = new GroovyClassLoader()
    if (item.kind == 'core') {
        loader.parseClass(new File(root, 'SignedLoanScope.groovy'))
        def scope = loader.loadClass('SignedLoanScope')
        try { output << [name:item.name,ok:true,result:scope.verify(item.payload,item.key,item.tool,item.now as long)] }
        catch (Exception e) { output << [name:item.name,ok:false,error:e.message] }
    } else {
        loader.parseClass('package org.apache.nifi.processor.io; interface InputStreamCallback { void process(java.io.InputStream input) }')
        loader.parseClass('package org.apache.nifi.processor.io; interface OutputStreamCallback { void process(java.io.OutputStream output) }')
        def session = new FakeSession(current:new FakeFile(attributes: item.attributes as Map,
                content:JsonOutput.toJson(item.payload).getBytes('UTF-8')))
        def bindings = new Binding([session:session, expectedTool:new Expando(value:item.tool), REL_SUCCESS:'success',REL_FAILURE:'failure'])
        new GroovyShell(loader,bindings).evaluate(new File(root,item.script))
        output << [name:item.name,relation:session.relation,attributes:session.current.attributes,
                   body:new JsonSlurper().parseText(new String(session.current.content,'UTF-8'))]
    }
}
println JsonOutput.toJson(output)
