import json
import streamlit as st
import time
import os
import json
import argparse
from tqdm import trange
import copy
import numpy as np
import torch
import random

from func_timeout import func_timeout, FunctionTimedOut
from PIL import Image

from agent.graph_dataset import __preprocess_item
from agent.model.graphtransformer.model import GraphormerEncoder
from agent.model.graphtransformer.model_args import ModelArgs
from agent.gen_vocab import reparse_graph_data

from reasoner import graph_solver, config
from reasoner.config import logger, eval_logger
from reasoner.graph_matching import load_models_from_json, get_model
from tool.run_HGR import get_graph_solver, solve_question, check_transformed_answer, evaluate_all_questions


DEBUG = False
random.seed(0)

EPSILON = 1e-5

map_dict = {}

node_type_vocab_file = 'agent/vocab/node_type_vocab.txt'
node_attr_vocab_file = 'agent/vocab/node_attr_vocab.txt'
edge_attr_vocab_file = 'agent/vocab/edge_attr_vocab.txt'
node_type_vocab = {line.strip(): i for i, line in enumerate(open(node_type_vocab_file, 'r').readlines())}
node_attr_vocab = {line.strip(): i for i, line in enumerate(open(node_attr_vocab_file, 'r').readlines())}
edge_attr_vocab = {line.strip(): i for i, line in enumerate(open(edge_attr_vocab_file, 'r').readlines())}

with open(config.diagram_logic_forms_json_path, 'r') as diagram_file:
    diagram_logic_forms_json = json.load(diagram_file)
with open(config.text_logic_forms_json_path, 'r') as text_file:
    text_logic_forms_json = json.load(text_file)

model_args = ModelArgs(num_classes=64, max_nodes=256, num_node_type=len(node_type_vocab),
                       num_node_attr=len(node_attr_vocab), num_in_degree=256, num_out_degree=256,
                       num_edges=len(edge_attr_vocab), num_spatial=20, num_edge_dis=256, edge_type="one_hop",
                       multi_hop_max_dist=1)

with open(config.error_ids_path, 'r') as file:
    error_ids = {line.strip() for line in file}
with open(config.model_pool_path, 'r') as model_pool_file:
    model_pool, model_id_map = load_models_from_json(json.load(model_pool_file))

model_save_path = ''
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
model = GraphormerEncoder(model_args).to(device)
if model_save_path:
    model.load_state_dict(torch.load(model_save_path, map_location=device))
model.eval()

def theorem_pred(graph_solver, model):
    global map_dict
    graph_data = graph_solver.global_graph.to_dict()
    graph_data, map_dict = reparse_graph_data(graph_data, map_dict)

    single_test_data = __preprocess_item(item=graph_data, node_type_vocab=node_type_vocab,
                                         node_attr_vocab=node_attr_vocab, edge_attr_vocab=edge_attr_vocab,
                                         spatial_pos_max=1)
    for k, v in single_test_data.items():
        single_test_data[k] = v.unsqueeze(0).to(device)
    output_logits = model(single_test_data)
    score = torch.softmax(output_logits, dim=-1).squeeze(0)
    sorted_score = torch.sort(score, descending=True)
    sorted_score_dict = {k.cpu().item(): v.cpu().item() for k, v in zip(sorted_score[1], sorted_score[0])}
    return sorted_score_dict


def beam_search(graph_solver, model, max_step, beam_size):
    t = 0
    hypotheses = [graph_solver]
    hyp_steps = [[]]
    hyp_scores = [0.]
    beam_search_res = {"answer": None, "step_lst": [], "model_instance_eq_num": None}

    while (t < max_step):
        t += 1
        hyp_num = len(hypotheses)
        assert hyp_num <= beam_size, f"hyp_num: {hyp_num}, beam_size: {beam_size}"

        hyp_theorem = []
        conti_hyp_scores = []
        conti_hyp_steps = []
        for hyp_index, hyp in enumerate(hypotheses):
            sorted_score_dict = theorem_pred(hyp, model)
            # print("step:", t, "past_steps:", hyp_steps[hyp_index], sorted_score_dict)
            for i in range(beam_size):
                cur_score = list(sorted_score_dict.values())[i]
                if cur_score < EPSILON:
                    continue
                hyp_theorem.append([hyp, list(sorted_score_dict.keys())[i]])
                conti_hyp_scores.append(hyp_scores[hyp_index] + np.log(cur_score))
                conti_hyp_steps.append(hyp_steps[hyp_index] + [list(sorted_score_dict.keys())[i]])

        conti_hyp_scores = torch.Tensor(conti_hyp_scores)
        top_cand_hyp_scores, top_cand_hyp_pos = torch.topk(conti_hyp_scores, k=min(beam_size, conti_hyp_scores.size(0)))

        new_hypotheses = []
        new_hyp_scores = []
        new_hyp_steps = []

        for cand_hyp_id, cand_hyp_score in zip(top_cand_hyp_pos, top_cand_hyp_scores):
            new_score = cand_hyp_score.detach().item()
            prev_hyp, theorem = hyp_theorem[cand_hyp_id]
            now_steps = conti_hyp_steps[cand_hyp_id]

            now_hyp = copy.deepcopy(prev_hyp)

            try:
                graph_model = get_model(model_pool, model_id_map, theorem)
                now_hyp.solve_with_one_model(graph_model)
                if not now_hyp.is_updated:
                    continue
            except Exception as e:
                eval_logger.error(e)
                continue

            if now_hyp.answer is not None:
                beam_search_res["answer"] = now_hyp.answer
                beam_search_res["step_lst"] = now_hyp.reasoning_record
                beam_search_res["model_instance_eq_num"] = now_hyp.model_instance_eq_num
                return beam_search_res

            new_hypotheses.append(now_hyp)
            new_hyp_scores.append(new_score)
            new_hyp_steps.append(now_steps)

        hypotheses = new_hypotheses
        hyp_scores = new_hyp_scores
        hyp_steps = new_hyp_steps

    return beam_search_res


def solve_with_time(q_id, model, max_step=10, beam_size=5):
    res = {"id": id, "target": None, "answer": None, "step_lst": [], "model_instance_eq_num": None, "correctness": "no",
           "time": None}

    try:
        res = func_timeout(120, solve, kwargs=dict(q_id=q_id, model=model, max_step=max_step, beam_size=beam_size))
        return res
    except FunctionTimedOut:
        eval_logger.error(f'q_id: {q_id} - Timeout')
        return res
    except Exception as e:
        eval_logger.error(f'q_id: {q_id} - Error: {e}')
        return res


def solve(q_id, model, max_step=10, beam_size=5):
    q_id = str(q_id)
    res = {"id": q_id, "target": None, "answer": None, "step_lst": [], "model_instance_eq_num": None,
           "correctness": "no", "time": None}
    s_time = time.time()

    if q_id not in diagram_logic_forms_json or q_id not in text_logic_forms_json or q_id in error_ids:
        logger.error(f"{q_id} not in diagram_logic_forms_json or not in text_logic_forms_json or in error_ids")
        return res

    data_path = os.path.join(config.db_dir_single, str(q_id), "data.json")
    with open(data_path, "r") as f:
        data = json.load(f)
    candidate_value_list = data['precise_value']
    gt_id = ord(data['answer']) - 65  # Convert A-D to 0-3
    try:
        graph_solver, target = get_graph_solver(q_id)
        graph_solver.init_solve()

        answer = graph_solver.answer
        if answer is not None:
            correctness, answer = check_transformed_answer(answer, candidate_value_list, gt_id)
            if correctness:
                res["correctness"] = "yes"
                res["target"] = target
                res["answer"] = answer
                res["model_instance_eq_num"] = graph_solver.model_instance_eq_num
                res['time'] = str(time.time() - s_time)
                return res

        beam_search_res = beam_search(graph_solver=graph_solver, model=model, max_step=max_step,beam_size=beam_size)
        answer = beam_search_res["answer"]
        res["step_lst"] = beam_search_res["step_lst"]
        res["model_instance_eq_num"] = beam_search_res["model_instance_eq_num"]
        res['time'] = str(time.time() - s_time)
        if answer is not None:
            correctness, answer = check_transformed_answer(answer, candidate_value_list, gt_id)
            if correctness:
                res["correctness"] = "yes"
                res["target"] = target
                res["answer"] = answer
                return res
        else:
            return res
    except FunctionTimedOut:
        eval_logger.error(f'q_id: {q_id} - Timeout')
        return res
    except Exception as e:
        eval_logger.error(f'q_id: {q_id} - Error: {e}')
        return res

st.title('Problem Solving')

index = st.sidebar.text_input(label='Problem ID')

if st.sidebar.button("Preview"):
    if index is not None:
        data = json.load(open("db/Geometry3K/" + str(index) + "/data.json"))
        st.sidebar.markdown(data['annotat_text'])
        choices = data['choices']
        choices_map = ['A', 'B', 'C', 'D']
        for i in range(len(choices)):
            st.sidebar.markdown(choices_map[i] + ". $" + choices[i] + "$")
        img_path = "db/Geometry3K/" + str(index) + "/img_diagram.png"
        image = Image.open(img_path)
        st.sidebar.image(image)

if st.sidebar.button("Solve"):
    if index is not None:

        data = json.load(open("db/Geometry3K/" + str(index) + "/data.json"))
        logic_form = json.load(open("db/Geometry3K/" + str(index) + "/logic_form.json"))
        text_logic_forms = logic_form["text_logic_form"]
        diagram_logic_forms = logic_form['diagram_logic_form']
        st.sidebar.markdown(data['annotat_text'])
        choices = data['choices']
        choices_map = ['A', 'B', 'C', 'D']
        for i in range(len(choices)):
            st.sidebar.markdown(choices_map[i] + ". $" + choices[i] + "$")
        img_path = "db/Geometry3K/" + str(index) + "/img_diagram_point.png"
        image = Image.open(img_path)
        st.sidebar.image(image)

        if len(text_logic_forms) > 1:
            st.subheader('Text Relations')
            for t in text_logic_forms[:-1]:
                st.markdown(t)

        st.subheader('Diagram Relations')
        for d in diagram_logic_forms:
            st.markdown(d)

        res = solve_with_time(index, model, max_step=10, beam_size=5)
        answer = res["answer"]
        step_lst = res["step_lst"]

        st.subheader('Target')
        st.markdown(res["target"])

        st.subheader('Solving Procedure')
        for i in range(len(step_lst)):
            step = step_lst[i]
            with st.expander('Step ' + str(i + 1) + ': ' + step["model_name"]):
                instances = step["instances"]
                for instance in instances:
                    relation = instance["relation"]
                    st.markdown(f"**{relation}**")
                    if len(instance["actions"]) > 0:
                        st.markdown('Actions:')
                        for action in instance["actions"]:
                            st.markdown(action)
                    if len(instance["equations"]) > 0:
                        st.markdown('Equations:')
                        for e in instance["equations"]:
                            st.markdown(e)
                # st.markdown('**Equations:**')
                # for e in equations_list[i]:
                #     st.text(e + " = 0")
                # st.markdown('**Solutions:**')
                # for key, value in solutions_list[i].items():
                #     st.text(key + " = " + value)

        st.subheader('Answer')
        st.markdown(str(res["answer"]))

        if answer:
            candi_answer = []
            for choice in data['precise_value']:
                candi_answer.append(abs(float(choice) - float(answer)))
            st.markdown('Choose **' + choices_map[candi_answer.index(min(candi_answer))] + "**")