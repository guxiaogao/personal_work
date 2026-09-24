package doa.reasoner;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.javalin.Javalin;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.io.StringDocumentSource;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.reasoner.InferenceType;
import org.semanticweb.owlapi.reasoner.OWLReasoner;

import java.util.*;

/**
 * 本体推理服务。Python 侧通过 HTTP 调用，本体以文本形式传入。
 *
 * <p>W2 端点规划：
 * <ul>
 *   <li>{@code /consistency}    —— 一致性检查 + 不可满足类（本次实现）
 *   <li>{@code /classify}       —— 分类，返回原子包含关系与类型断言
 *   <li>{@code /conservativity} —— 旧签名上的推论 diff，二维定级的 L0 判据
 * </ul>
 *
 * <p>服务本身不持久化任何状态：每次请求现场构造本体、推理、丢弃。
 * 本体的真正来源是 Python 侧的 SchemaRegistry。
 */
public final class App {

    private static final ObjectMapper JSON = new ObjectMapper();

    public static void main(String[] args) {
        int port = Integer.parseInt(
                Optional.ofNullable(System.getenv("DOA_REASONER_PORT")).orElse("7070"));

        Javalin app = Javalin.create(cfg -> cfg.showJavalinBanner = false).start(port);

        app.get("/health", ctx -> ctx.json(Map.of(
                "status", "ok",
                "reasoner", "HermiT",
                "owlapi", OWLManager.class.getPackage().getImplementationVersion() == null
                        ? "5.1.9" : OWLManager.class.getPackage().getImplementationVersion())));

        app.post("/consistency", ctx -> {
            Req req = JSON.readValue(ctx.body(), Req.class);
            if (req.ontology == null || req.ontology.isBlank()) {
                ctx.status(400).json(Map.of("error", "缺少 ontology 字段"));
                return;
            }
            try {
                ctx.json(checkConsistency(req.ontology));
            } catch (OWLOntologyCreationException e) {
                // 本体文本本身解析失败，属调用方错误
                ctx.status(400).json(Map.of("error", "本体解析失败: " + e.getMessage()));
            }
        });

        System.out.println("reasoner-service 已启动 :" + port);
    }

    /** 请求体。ontology 为 OWL 文本，格式由 OWLAPI 自动识别（函数式语法 / RDF-XML / Turtle 等）。 */
    static final class Req {
        public String ontology;
    }

    static Map<String, Object> checkConsistency(String ontologyText)
            throws OWLOntologyCreationException {

        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        OWLOntology ont = manager.loadOntologyFromOntologyDocument(
                new StringDocumentSource(ontologyText));

        long t0 = System.nanoTime();
        OWLReasoner reasoner = new ReasonerFactory().createReasoner(ont);
        try {
            boolean consistent = reasoner.isConsistent();

            List<String> unsat = new ArrayList<>();
            if (consistent) {
                // 本体不一致时 getUnsatisfiableClasses 会抛异常，故仅在一致时计算。
                // 不一致 ⇒ 所有类都不可满足，逐个列举没有意义。
                reasoner.precomputeInferences(InferenceType.CLASS_HIERARCHY);
                for (OWLClass c : reasoner.getUnsatisfiableClasses().getEntities()) {
                    if (!c.isOWLNothing()) {
                        unsat.add(c.getIRI().toString());
                    }
                }
                Collections.sort(unsat);
            }

            long elapsedMs = (System.nanoTime() - t0) / 1_000_000;

            Map<String, Object> out = new LinkedHashMap<>();
            out.put("consistent", consistent);
            out.put("unsatisfiableClasses", unsat);
            out.put("axiomCount", ont.getAxiomCount());
            out.put("classCount", ont.getClassesInSignature().size());
            out.put("reasonerTimeMs", elapsedMs);
            return out;
        } finally {
            reasoner.dispose();
            manager.removeOntology(ont);
        }
    }

    private App() {}
}
